"""Staff commands, sent to the bot's number from a registered staff phone.

    RESUME <phone>              hand a paused chat back to the bot
    RESUME                      list paused chats
    LEAD <phone> <what they want>   save a yard-visitor lead and ask the customer to opt in

Only messages that START with a command word count, and only from staff numbers; anything
else from staff goes to the agent like a customer message (handy for testing).
"""

from __future__ import annotations

import re

from app.core.client_config import ClientConfig
from app.core.clock import Clock
from app.core.phone import try_normalize_phone
from app.leads.parse import is_lead_command
from app.leads.service import LeadService
from app.storage.repository import Repository

_RESUME_RE = re.compile(r"^\s*resume\s*:?(?=\s|$)(.*)$", re.IGNORECASE | re.DOTALL)
_HELP_RE = re.compile(r"^\s*(help|commands)\s*\??\s*$", re.IGNORECASE)

HELP = ("Staff commands:\n"
        "RESUME <phone> — hand a paused chat back to the bot\n"
        "RESUME — list paused chats\n"
        "LEAD <phone> <what they want> — e.g. LEAD 98XXXXXXXX Swift 6L petrol under 60000km")


class StaffCommands:
    def __init__(self, config: ClientConfig, repo: Repository, clock: Clock, leads: LeadService) -> None:
        self.config = config
        self.repo = repo
        self.clock = clock
        self.leads = leads

    def handle(self, staff_phone: str, text: str) -> str | None:
        """The reply for the staff member, or None if this isn't a command."""
        if not self.config.is_staff(staff_phone):
            return None
        if is_lead_command(text):
            return self.leads.create_from_command(staff_phone, text)
        if match := _RESUME_RE.match(text):
            return self._resume(match.group(1).strip())
        if _HELP_RE.match(text):
            return HELP
        return None

    def _resume(self, arg: str) -> str:
        cid = self.config.client_id
        if not arg:
            paused = self.repo.list_paused_customers(cid)
            if not paused:
                return "No paused chats right now."
            return "Paused chats:\n" + "\n".join(
                f"{c.phone}{f' ({c.name})' if c.name else ''}: {c.paused_reason or '-'}" for c in paused)
        phone = try_normalize_phone(arg.replace(" ", ""))
        if phone is None:
            return f"❌ Couldn't read the phone number '{arg}'. Use: RESUME 98XXXXXXXX"
        customer = self.repo.get_customer(cid, phone)
        if customer is None or not customer.bot_paused:
            return f"The bot isn't paused for {phone}; nothing to do."
        self.repo.set_bot_paused(cid, phone, False, at=self.clock.now(), reason="staff_resume")
        return f"✅ The bot is answering {phone} again."
