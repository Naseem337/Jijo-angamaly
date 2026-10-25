"""MessageRouter: what happens to each inbound WhatsApp message (PLAN.md §1).

Order: dedupe → staff command (RESUME / LEAD, staff numbers only) → keywords (STOP, START,
YES to a lead's confirmation) → paused chat (store only; auto-resumes after 24 h)
→ voice note (→ text, then the same keywords) / other media (polite "text or voice please")
→ agent → send reply → handoff (pause + staff summary) if the agent asked for one.

Staff messages that aren't commands go to the agent like a customer's, so staff can test
the bot (on Meta's test number, only registered numbers can message it at all).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Literal

from app.agent import Agent, AgentReply
from app.conversation.voice_in import VOICE_PROBLEM_REPLY, VoiceIn
from app.conversation.optout import opt_in_confirmation, opt_out_confirmation, reply_language
from app.conversation.photos import PhotoSender
from app.conversation.voice_out import VoiceOut
from app.core.client_config import ClientConfig
from app.core.clock import Clock
from app.core.language import classify_confirmation, is_emoji_only, is_opt_in, is_opt_out
from app.core.phone import mask_phone
from app.handoff.handoff import HandoffService
from app.handoff.staff_commands import StaffCommands
from app.leads.service import LeadService
from app.storage.models import Customer, Message
from app.storage.repository import Repository
from app.whatsapp.client import WhatsAppClient, WhatsAppError
from app.whatsapp.parse import InboundMessage

log = logging.getLogger(__name__)

Outcome = Literal["duplicate", "staff_command", "opted_out", "opted_in", "lead_consent", "paused", "ignored",
                  "unsupported", "voice_problem", "replied", "send_failed"]

SILENT_TYPES = frozenset({"reaction", "sticker"})  # never answered

UNSUPPORTED_REPLY = {
    "ml": "ഇത് തുറക്കാൻ പറ്റില്ല, ടെക്സ്റ്റോ വോയ്‌സോ അയക്കാമോ?",
    "manglish": "Ithu enikku kaanan pattilla, text-o voice-o ayakkamo?",
    "en": "I can't open this, can you send text or a voice note?",
}


_DETAIL_LINE = re.compile(r"₹|\(\s*[A-Z]{2,4}-[0-9A-Z]{3,}\s*\)")  # a price or a car id: car data, not talk


def car_details_text(text: str) -> str:
    """Just the lines that carry car data (car lines, prices, km, EMI), for the text that goes
    with a voice reply. The talking around them is in the voice note already."""
    return "\n".join(line for line in text.splitlines() if _DETAIL_LINE.search(line)).strip()


class MessageRouter:
    def __init__(self, config: ClientConfig, repo: Repository, agent: Callable[[], Agent],
                 whatsapp: WhatsAppClient, clock: Clock,
                 voice_in: Callable[[], VoiceIn | None] = lambda: None,
                 voice_out: Callable[[], VoiceOut | None] = lambda: None, *,
                 staff: StaffCommands | None = None, leads: LeadService | None = None,
                 handoff: HandoffService | None = None,
                 photos: Callable[[], PhotoSender | None] = lambda: None) -> None:
        self.config = config
        self.repo = repo
        self._agent = agent  # built lazily, so a missing LLM key doesn't break the webhook
        self.whatsapp = whatsapp
        self.clock = clock
        self._voice_in = voice_in  # returns None when speech-to-text isn't configured
        self._voice_out = voice_out  # returns None when TTS or ffmpeg isn't available
        self.staff = staff
        self.leads = leads
        self.handoff = handoff
        self._photos = photos  # returns None without ffmpeg

    def handle(self, msg: InboundMessage) -> Outcome:
        cid, phone, now = self.config.client_id, msg.phone, self.clock.now()
        if not self.repo.mark_event_processed(cid, msg.id, now):
            log.info("Duplicate delivery %s ignored", msg.id)
            return "duplicate"

        if self.staff and msg.is_text and self.config.is_staff(phone):
            staff_reply = self.staff.handle(phone, msg.text)
            if staff_reply is not None:
                self.repo.add_message(Message(cid, phone, "in", msg.text, now, source="staff", id=msg.id))
                return "staff_command" if self._send(phone, staff_reply, store=True) else "send_failed"

        customer = self.repo.get_or_create_customer(cid, phone, now)
        if msg.type in SILENT_TYPES or (msg.is_text and is_emoji_only(msg.text)
                                        and not self.repo.get_pending_action(cid, phone)):
            # A reaction, sticker or lone emoji needs no answer (user, 2026-10-21): keep a record, say nothing.
            self.repo.add_message(Message(cid, phone, "in", msg.text or f"[{msg.type}]", now, id=msg.id))
            return "ignored"
        if msg.is_text and (outcome := self._keywords(customer, msg.text, msg.id, now, "text")):
            return outcome
        if customer.bot_paused and not (self.handoff and self.handoff.resume_if_due(customer, now)):
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
                return "voice_problem" if ok else "send_failed"  # text only: no voice for problems
            text, source = result.text, "voice"
            if outcome := self._keywords(customer, text, msg.id, now, "voice"):
                return outcome
        elif not msg.is_text:
            self.repo.add_message(Message(cid, phone, "in", msg.text or f"[{msg.type}]", now, id=msg.id))
            reply_text = UNSUPPORTED_REPLY.get(customer.language, UNSUPPORTED_REPLY["en"])
            return "unsupported" if self._send(phone, reply_text, store=True) else "send_failed"
        else:
            self.whatsapp.mark_read(msg.id, typing=True)

        voice_out = self._voice_out()
        want_voice = bool(voice_out and voice_out.wants_voice(source, text))
        reply = self._agent().handle(phone, text, source=source, message_id=msg.id, want_voice=want_voice,
                                     profile_name=msg.profile_name)
        if reply.handoff:
            if self.handoff:
                self.handoff.handle(customer, reply.handoff, msg.profile_name)
            else:
                log.warning("Handoff requested for %s but no handoff service: %s", mask_phone(phone),
                            reply.handoff.reason)
        outcome = self._send_reply(phone, reply, voice_out)
        if outcome == "replied" and reply.photos:
            sender = self._photos()
            for car_id in reply.photos:  # after the words, like a salesman forwarding pictures
                if not (sender and sender.send(phone, car_id)):
                    log.warning("Photo of %s not sent to %s", car_id, mask_phone(phone))
        return outcome

    def _send_reply(self, phone: str, reply: AgentReply, voice_out: VoiceOut | None) -> Outcome:
        """Text in → text out. Voice in → voice out, never the same words again as text (user rule,
        2026-10-20); only the car details (list, prices, EMI figures) follow as text to read. If the
        voice note fails, the full text goes instead, so the customer always gets an answer."""
        details = car_details_text(reply.text) if reply.has_car_details else ""
        if voice_out and reply.spoken_text:
            # Cars: the list goes FIRST as text, then a short voice note that names the cars and
            # budgets (user, 2026-10-21). Otherwise the voice note is the whole reply.
            if details and not self._send(phone, details, store=False):
                return "send_failed"
            if voice_out.send(phone, reply.spoken_text, reply.language):
                return "replied"
            # The voice failed: send the rest of the text (the list already went out).
            listed = details.splitlines()
            rest = "\n".join(line for line in reply.text.splitlines() if line.strip() and line not in listed)
            if details and not rest:
                return "replied"
            return "replied" if self._send(phone, rest or reply.text, store=False) else "send_failed"
        return "replied" if self._send(phone, reply.text, store=False) else "send_failed"

    def _keywords(self, customer: Customer, text: str, msg_id: str, now, source: str) -> Outcome | None:
        """STOP, START and YES-to-a-lead are handled here, before the agent (and even while paused)."""
        cid, phone = self.config.client_id, customer.phone
        language = reply_language(text, customer.language)

        def store_in() -> None:
            self.repo.add_message(Message(cid, phone, "in", text, now, source=source, id=msg_id))

        if is_opt_out(text, self.config.business_rules.opt_out_extra_phrases):
            store_in()
            first_time = not customer.opted_out
            if first_time:
                self.repo.set_opted_out(cid, phone, now)
            if self.leads:
                self.leads.opt_out_all(phone)
            log.info("Opt-out from %s (first time: %s)", mask_phone(phone), first_time)
            if first_time:  # confirmed once; a repeated STOP gets no reply
                self._send(phone, opt_out_confirmation(self.config, language), store=True)
            return "opted_out"
        if customer.opted_out and is_opt_in(text):
            store_in()
            self.repo.set_opted_out(cid, phone, None)
            if self.leads:
                self.leads.opt_in_again(phone)
            self._send(phone, opt_in_confirmation(self.config, language), store=True)
            return "opted_in"
        if self.leads and classify_confirmation(text) == "yes" and self.leads.has_pending_consent(phone):
            store_in()
            customer.language = language
            self._send(phone, self.leads.accept_consent(customer), store=True)
            return "lead_consent"
        return None

    def _send(self, phone: str, text: str, *, store: bool) -> bool:
        try:
            self.whatsapp.send_text(phone, text)
        except WhatsAppError as exc:
            log.error("Could not send to %s: %s", mask_phone(phone), exc)
            return False
        if store:  # agent replies are stored by the agent itself
            self.repo.add_message(Message(self.config.client_id, phone, "out", text, self.clock.now()))
        return True
