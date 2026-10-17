"""MessageRouter: what happens to each inbound WhatsApp message (PLAN.md §1).

Order: dedupe → staff command (step 9) → opt-out (step 9) → paused chat (store only)
→ non-text (voice arrives in step 7) → agent → send reply.

Until step 9, staff numbers are treated like customers so the bot can be tested from
them (on Meta's test number, only registered numbers can message the bot at all).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from app.agent import Agent
from app.core.client_config import ClientConfig
from app.core.clock import Clock
from app.core.phone import mask_phone
from app.storage.models import Message
from app.storage.repository import Repository
from app.whatsapp.client import WhatsAppClient, WhatsAppError
from app.whatsapp.parse import InboundMessage

log = logging.getLogger(__name__)

Outcome = Literal["duplicate", "paused", "unsupported", "replied", "send_failed"]

UNSUPPORTED_REPLY = {
    "ml": "ക്ഷമിക്കണം, ഇപ്പോൾ എനിക്ക് ടെക്സ്റ്റ് മെസ്സേജുകൾ മാത്രമേ വായിക്കാൻ കഴിയൂ. ദയവായി ടൈപ്പ് ചെയ്ത് അയയ്ക്കാമോ?",
    "manglish": "Sorry, ippol enikku text messages mathrame vayikkan pattu. Onnu type cheythu ayakkamo?",
    "en": "Sorry, I can only read text messages for now. Could you type your question?",
}


class MessageRouter:
    def __init__(self, config: ClientConfig, repo: Repository, agent: Callable[[], Agent],
                 whatsapp: WhatsAppClient, clock: Clock) -> None:
        self.config = config
        self.repo = repo
        self._agent = agent  # built lazily, so a missing LLM key doesn't break the webhook
        self.whatsapp = whatsapp
        self.clock = clock

    def handle(self, msg: InboundMessage) -> Outcome:
        cid, phone, now = self.config.client_id, msg.phone, self.clock.now()
        if not self.repo.mark_event_processed(cid, msg.id, now):
            log.info("Duplicate delivery %s ignored", msg.id)
            return "duplicate"

        customer = self.repo.get_or_create_customer(cid, phone, now)
        if customer.bot_paused:
            # A salesperson owns this chat; keep the message for the dashboard only.
            self.repo.add_message(Message(cid, phone, "in", msg.text or f"[{msg.type}]", now, id=msg.id))
            return "paused"

        if not msg.is_text:
            self.repo.add_message(Message(cid, phone, "in", msg.text or f"[{msg.type}]", now,
                                          source="voice" if msg.type == "audio" else "text", id=msg.id))
            text = UNSUPPORTED_REPLY.get(customer.language, UNSUPPORTED_REPLY["en"])
            return "unsupported" if self._send(phone, text, store=True) else "send_failed"

        self.whatsapp.mark_read(msg.id, typing=True)
        reply = self._agent().handle(phone, msg.text, source="text", message_id=msg.id)
        if reply.handoff:
            log.warning("Handoff requested for %s: %s (staff notification arrives in step 9)",
                        mask_phone(phone), reply.handoff.reason)
        return "replied" if self._send(phone, reply.text, store=False) else "send_failed"

    def _send(self, phone: str, text: str, *, store: bool) -> bool:
        try:
            self.whatsapp.send_text(phone, text)
        except WhatsAppError as exc:
            log.error("Could not send to %s: %s", mask_phone(phone), exc)
            return False
        if store:  # agent replies are stored by the agent itself
            self.repo.add_message(Message(self.config.client_id, phone, "out", text, self.clock.now()))
        return True
