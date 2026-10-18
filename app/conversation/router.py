"""MessageRouter: what happens to each inbound WhatsApp message (PLAN.md §1).

Order: dedupe → staff command (step 9) → opt-out (step 9) → paused chat (store only)
→ voice note (→ text via speech-to-text) / other media (polite "text or voice please")
→ agent → send reply.

Until step 9, staff numbers are treated like customers so the bot can be tested from
them (on Meta's test number, only registered numbers can message the bot at all).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from app.agent import Agent
from app.conversation.voice_in import VOICE_PROBLEM_REPLY, VoiceIn
from app.conversation.voice_out import VoiceOut
from app.core.client_config import ClientConfig
from app.core.clock import Clock
from app.core.phone import mask_phone
from app.storage.models import Message
from app.storage.repository import Repository
from app.whatsapp.client import WhatsAppClient, WhatsAppError
from app.whatsapp.parse import InboundMessage

log = logging.getLogger(__name__)

Outcome = Literal["duplicate", "paused", "unsupported", "voice_problem", "replied", "send_failed"]

UNSUPPORTED_REPLY = {
    "ml": "ക്ഷമിക്കണം, എനിക്ക് ടെക്സ്റ്റ്, വോയ്‌സ് മെസ്സേജുകൾ മാത്രമേ മനസ്സിലാകൂ. ദയവായി ടൈപ്പ് ചെയ്യുകയോ പറയുകയോ ചെയ്യാമോ?",
    "manglish": "Sorry, enikku text-um voice message-um mathrame manasilaakoo. Onnu type cheyyukayo parayukayo cheyyamo?",
    "en": "Sorry, I can only understand text and voice messages. Could you type or say your question?",
}


class MessageRouter:
    def __init__(self, config: ClientConfig, repo: Repository, agent: Callable[[], Agent],
                 whatsapp: WhatsAppClient, clock: Clock,
                 voice_in: Callable[[], VoiceIn | None] = lambda: None,
                 voice_out: Callable[[], VoiceOut | None] = lambda: None) -> None:
        self.config = config
        self.repo = repo
        self._agent = agent  # built lazily, so a missing LLM key doesn't break the webhook
        self.whatsapp = whatsapp
        self.clock = clock
        self._voice_in = voice_in  # returns None when speech-to-text isn't configured
        self._voice_out = voice_out  # returns None when TTS or ffmpeg isn't available

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

        text, source = msg.text, "text"
        if msg.type == "audio":
            self.whatsapp.mark_read(msg.id, typing=True)
            voice = self._voice_in()
            result = voice.process(msg.media_id) if voice else None
            if result is None or result.text is None:
                problem = result.problem if result else "failed"
                log.info("Voice note from %s not used: %s %s", mask_phone(phone), problem,
                         result.detail if result else "(speech-to-text not configured)")
                self.repo.add_message(Message(cid, phone, "in", f"[voice note: {problem}]", now,
                                              source="voice", id=msg.id))
                replies = VOICE_PROBLEM_REPLY[problem]
                ok = self._send(phone, replies.get(customer.language, replies["en"]), store=True)
                voice_out = self._voice_out()
                if ok and problem != "failed" and voice_out and voice_out.wants_voice("voice"):
                    spoken_language = "en" if customer.language == "en" else "ml"
                    voice_out.send(phone, replies[spoken_language], spoken_language)
                return "voice_problem" if ok else "send_failed"
            text, source = result.text, "voice"
        elif not msg.is_text:
            self.repo.add_message(Message(cid, phone, "in", msg.text or f"[{msg.type}]", now, id=msg.id))
            reply_text = UNSUPPORTED_REPLY.get(customer.language, UNSUPPORTED_REPLY["en"])
            return "unsupported" if self._send(phone, reply_text, store=True) else "send_failed"
        else:
            self.whatsapp.mark_read(msg.id, typing=True)

        voice_out = self._voice_out()
        want_voice = bool(voice_out and voice_out.wants_voice(source))
        reply = self._agent().handle(phone, text, source=source, message_id=msg.id, want_voice=want_voice)
        if reply.handoff:
            log.warning("Handoff requested for %s: %s (staff notification arrives in step 9)",
                        mask_phone(phone), reply.handoff.reason)
        if not self._send(phone, reply.text, store=False):
            return "send_failed"
        if voice_out and reply.spoken_text:
            voice_out.send(phone, reply.spoken_text, reply.language)  # text already sent; best effort
        return "replied"

    def _send(self, phone: str, text: str, *, store: bool) -> bool:
        try:
            self.whatsapp.send_text(phone, text)
        except WhatsAppError as exc:
            log.error("Could not send to %s: %s", mask_phone(phone), exc)
            return False
        if store:  # agent replies are stored by the agent itself
            self.repo.add_message(Message(self.config.client_id, phone, "out", text, self.clock.now()))
        return True
