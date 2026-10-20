"""Yard-visitor leads: staff create them, the customer confirms, STOP/START control them.

Lifecycle (status):  pending_consent --YES--> active --(step 10 alert)--> notified
                     any open --STOP--> opted_out --START--> active (or pending again)
                     open past expires_at --> expired
Step 10 (alerts) reads `list_leads(status="active")` and `lead.requirement`, and moves a lead
to `notified`; nothing here needs to change for that.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from app.core.client_config import ClientConfig
from app.core.clock import Clock
from app.core.phone import mask_phone
from app.leads.parse import extract_requirement, is_lead_command, split_lead_command
from app.providers.llm import LLMProvider
from app.storage.models import OPEN_LEAD_STATUSES, Customer, Lead, LeadRequirement, Message
from app.storage.repository import Repository
from app.whatsapp.client import WhatsAppClient, WhatsAppError

log = logging.getLogger(__name__)

SERVICE_WINDOW = timedelta(hours=24)  # WhatsApp: free-form messages only within 24 h of the customer's last message
USAGE = "Use: LEAD <phone> <what they want>, e.g. LEAD 98XXXXXXXX Swift 6L petrol under 60000km"


def in_service_window(customer: Customer | None, now: datetime) -> bool:
    return bool(customer and customer.last_inbound_at and now - customer.last_inbound_at < SERVICE_WINDOW)


def _short_place(config: ClientConfig) -> str:
    return config.city.split(",")[0].strip()


def consent_request(config: ClientConfig, requirement: LeadRequirement, language: str | None) -> str:
    """The ONE message asking the customer to opt in: plain words, in their language.
    language=None (never chatted with us) → Malayalam and English together."""
    place, name = _short_place(config), config.display_name
    texts = {
        "ml": (f"നമസ്കാരം, {place} {name}-ൽ നിന്നാണ്. നിങ്ങൾ നോക്കുന്ന {requirement.describe('ml')} "
               "വണ്ടി വന്നാൽ WhatsApp-ൽ അറിയിക്കട്ടെ? വേണമെങ്കിൽ YES, വേണ്ടെങ്കിൽ STOP എന്ന് മറുപടി തന്നാൽ മതി."),
        "manglish": (f"Hi, {place} {name}-il ninnu aanu. Ningal nokkunna {requirement.describe('manglish')} "
                     "vandi vannal WhatsApp-il ariyikkatte? Venel YES, venda enkil STOP ennu reply cheythal mathi."),
        "en": (f"Hi, this is {name}, {place}. You're looking for: {requirement.describe('en')}. Shall we "
               "WhatsApp you when one comes in? Just reply YES, or STOP if you'd rather not."),
    }
    if language in texts:
        return texts[language]
    return f"{texts['ml']}\n\n{texts['en']}"


CONSENT_THANKS = {
    "ml": "നന്ദി! ഇതുപോലൊരു കാർ വന്നാൽ ഞങ്ങൾ WhatsApp-ൽ അറിയിക്കാം. വേണ്ടെങ്കിൽ എപ്പോൾ വേണമെങ്കിലും STOP എന്ന് അയച്ചാൽ മതി.",
    "manglish": "Thanks! Ithupoloru car vannal njangal WhatsApp-il ariyikkam. Venda enkil eppo venelum STOP ennu ayachal mathi.",
    "en": "Thanks! We'll WhatsApp you when a matching car arrives. Reply STOP anytime to stop these alerts.",
}


class LeadService:
    def __init__(self, config: ClientConfig, repo: Repository, whatsapp: WhatsAppClient, clock: Clock,
                 llm: Callable[[], LLMProvider | None] = lambda: None,
                 known_models: Callable[[], Iterable[str]] = lambda: ()) -> None:
        self.config = config
        self.repo = repo
        self.whatsapp = whatsapp
        self.clock = clock
        self._llm = llm  # lazy: a missing AI key must not stop the rule-based parser
        self._known_models = known_models  # model names in stock, to recognise them in staff notes

    # ------------------------------------------------------------------ staff: LEAD <phone> <text>

    def create_from_command(self, staff_phone: str, text: str) -> str:
        """Handle a staff LEAD message; returns the reply for the staff member."""
        if not is_lead_command(text):
            return USAGE
        command = split_lead_command(text)
        if command.phone is None:
            shown = f"'{command.raw}'" if command.raw else "nothing"
            return f"❌ Not saved: couldn't read a phone number ({shown}). {USAGE}"
        if not command.text.strip():
            return f"❌ Not saved: say what the customer wants after the number. {USAGE}"
        try:
            llm = self._llm()
        except Exception as exc:  # no AI configured: the rule-based reader still works
            log.warning("LEAD parsing without AI: %s", exc)
            llm = None
        try:
            known = list(self._known_models())
        except Exception:  # stock unavailable: aliases still help
            known = []
        requirement, name, _ = extract_requirement(llm, command.text, self.config.business_rules.model_aliases, known)
        if requirement.is_empty():
            return ("❌ Not saved: couldn't find what the customer wants. Add a model, budget, type or fuel, "
                    f"e.g. LEAD {command.phone} Swift 6L petrol")

        now, cid, phone = self.clock.now(), self.config.client_id, command.phone
        customer = self.repo.get_customer(cid, phone)
        if customer and customer.opted_out:
            return (f"❌ Not saved: {phone} has opted out of WhatsApp alerts. They can reply START to "
                    "the bot to turn them back on; then send LEAD again.")

        existing = self.repo.find_open_lead(cid, phone)
        expires = now + timedelta(days=self.config.leads.expiry_days)
        if existing:
            existing.requirement, existing.expires_at, existing.updated_at = requirement, expires, now
            existing.name = name or existing.name
            self.repo.update_lead(existing)
            lead, verb = existing, "Updated the existing lead"
        else:
            lead = Lead(cid, phone, now, expires, staff_phone, requirement, name=name)
            self.repo.create_lead(lead)
            verb = "Saved new lead"
        summary = requirement.summary()
        line = f"✅ {verb} for {phone}{f' ({lead.name})' if lead.name else ''}: {summary}."
        if lead.status == "active":
            return f"{line} The customer already said YES, so alerts stay on."
        return f"{line} {self._ask_consent(lead, customer, summary)}"

    def _ask_consent(self, lead: Lead, customer: Customer | None, summary: str) -> str:
        now = self.clock.now()
        if not in_service_window(customer, now):
            log.info("Lead %s: customer %s outside the 24 h window, template needed", lead.id, mask_phone(lead.phone))
            return ("Confirmation NOT sent: the customer hasn't messaged us in the last 24 h, so WhatsApp only "
                    "allows an approved template (template needed; that comes in step 10). Tip: ask them to "
                    "send 'Hi' to our number, then send LEAD again.")
        text = consent_request(self.config, lead.requirement, customer.language if customer else None)
        try:
            self.whatsapp.send_text(lead.phone, text)
        except WhatsAppError as exc:
            log.error("Lead consent request to %s failed: %s", mask_phone(lead.phone), exc)
            return "Confirmation could not be sent (WhatsApp error); try again later."
        self.repo.add_message(Message(self.config.client_id, lead.phone, "out", text, now, source="system",
                                      meta={"lead_id": lead.id, "kind": "lead_consent_request"}))
        return "Asked the customer to reply YES to get alerts."

    # ------------------------------------------------------------------ customer replies

    def has_pending_consent(self, phone: str) -> bool:
        lead = self.repo.find_open_lead(self.config.client_id, phone)
        return bool(lead and lead.status == "pending_consent" and lead.expires_at > self.clock.now())

    def accept_consent(self, customer: Customer) -> str:
        """The customer replied YES: activate their pending leads. Returns the thank-you text."""
        now, cid = self.clock.now(), self.config.client_id
        changed = self.repo.set_leads_status(cid, customer.phone, ("pending_consent",), "active", now, consent_at=now)
        self.repo.set_consent(cid, customer.phone, source="chat", recorded_by="customer", at=now)
        log.info("Customer %s said YES: %d lead(s) active", mask_phone(customer.phone), changed)
        return CONSENT_THANKS.get(customer.language, CONSENT_THANKS["en"])

    def opt_out_all(self, phone: str) -> int:
        """STOP applies to every lead the customer has (pending, active or already alerted)."""
        return self.repo.set_leads_status(self.config.client_id, phone,
                                          (*OPEN_LEAD_STATUSES, "notified"), "opted_out", self.clock.now())

    def opt_in_again(self, phone: str) -> int:
        """START after STOP: leads they had agreed to come back on; the others wait for a YES again."""
        now, changed = self.clock.now(), 0
        for lead in self.repo.list_leads(self.config.client_id, status="opted_out", phone=phone):
            if lead.expires_at <= now:
                continue
            lead.status, lead.updated_at = ("active" if lead.consent_at else "pending_consent"), now
            self.repo.update_lead(lead)
            changed += 1
        return changed
