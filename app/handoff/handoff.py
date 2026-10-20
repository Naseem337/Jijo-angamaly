"""Handing a chat to the sales team (PLAN.md step 9).

When the agent asks for a handoff, the customer's chat is paused (the bot stays quiet and
only stores their messages), and every staff number gets a short summary. The customer has
already been told by the agent's reply that the team will contact them.

  - Staff who messaged the bot in the last 24 h get a normal text; others need the
    `staff_lead_alert` template (Meta approval pending; a failure is logged).
  - System handoffs (AI outage, guardrail fallback) notify staff but don't pause the bot,
    so it can answer again as soon as it recovers.
  - Staff numbers are never paused: on Meta's test number staff also play the customer.
  - A paused chat resumes after `bot.auto_resume_hours` (24 h), or at once with RESUME.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.agent.tools import HandoffRequest
from app.core.client_config import ClientConfig
from app.core.clock import Clock, ist_today
from app.core.phone import mask_phone, to_wa_id
from app.leads.service import in_service_window
from app.storage.models import Customer
from app.storage.repository import Repository
from app.whatsapp.client import WhatsAppClient, WhatsAppError

log = logging.getLogger(__name__)

SYSTEM_REASONS = frozenset({"llm_unavailable", "guardrail"})


class HandoffService:
    def __init__(self, config: ClientConfig, repo: Repository, whatsapp: WhatsAppClient, clock: Clock) -> None:
        self.config = config
        self.repo = repo
        self.whatsapp = whatsapp
        self.clock = clock

    def handle(self, customer: Customer, request: HandoffRequest, profile_name: str | None = None) -> bool:
        """Pause (unless system/staff) and notify staff. Returns True if the chat was paused."""
        now, cid = self.clock.now(), self.config.client_id
        system = request.reason in SYSTEM_REASONS
        pause = not system and not self.config.is_staff(customer.phone)
        if pause:
            self.repo.set_bot_paused(cid, customer.phone, True, at=now, reason=request.reason)
        # An outage would otherwise alert staff on every message: one notice per customer per day.
        if not system or self.repo.increment_counter(cid, f"system_notice:{request.reason}:{customer.phone}",
                                                     ist_today(self.clock).isoformat(), cap=1):
            self.notify_staff(customer, request, profile_name, paused=pause)
        log.info("Handoff for %s (%s); paused=%s", mask_phone(customer.phone), request.reason, pause)
        return pause

    def notify_staff(self, customer: Customer, request: HandoffRequest, profile_name: str | None,
                     *, paused: bool) -> None:
        now = self.clock.now()
        who = customer.name or profile_name or "customer"
        lines = [f"🔔 {who} ({customer.phone}) needs the team.",
                 f"Reason: {request.reason}"]
        if request.summary:
            lines.append(f"Wants: {request.summary}")
        lines.append(f"Chat: https://wa.me/{to_wa_id(customer.phone)}")
        lines.append(f"The bot is paused for them; reply RESUME {customer.phone} to hand it back." if paused
                     else "The bot keeps answering them.")
        text = "\n".join(lines)
        for staff in self.config.staff_numbers:
            staff_record = self.repo.get_customer(self.config.client_id, staff)
            try:
                if in_service_window(staff_record, now):
                    self.whatsapp.send_text(staff, text)
                    continue
                template = self.config.whatsapp.templates.get("staff_lead_alert")
                if not template:
                    log.warning("Staff %s outside 24 h window and no staff template configured", mask_phone(staff))
                    continue
                self.whatsapp.send_template(staff, template, "en", [
                    who, customer.phone, (request.summary or "-")[:200], "-", request.reason[:100]])
            except WhatsAppError as exc:
                log.error("Staff notification to %s failed (template needed until approved): %s",
                          mask_phone(staff), exc)

    def resume_if_due(self, customer: Customer, now: datetime) -> bool:
        """Auto-resume a paused chat after `bot.auto_resume_hours`. Returns True if resumed."""
        hours = self.config.business_rules.bot.auto_resume_hours
        if customer.bot_paused and customer.paused_at and now - customer.paused_at >= timedelta(hours=hours):
            self.repo.set_bot_paused(self.config.client_id, customer.phone, False, at=now, reason="auto_resume")
            customer.bot_paused = False
            log.info("Auto-resumed bot for %s after %d h", mask_phone(customer.phone), hours)
            return True
        return False

    def auto_resume_all(self) -> int:
        """For a scheduled sweep (AWS, step 13): resume every chat paused for too long."""
        now = self.clock.now()
        return sum(self.resume_if_due(c, now) for c in self.repo.list_paused_customers(self.config.client_id))
