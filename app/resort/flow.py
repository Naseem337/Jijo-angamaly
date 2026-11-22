"""The guest conversation: free text in any language, answered by the AI (see chat.py), plus payments.

There are no menus, lists or buttons in the booking chat. A plain greeting gets one question (which
place); everything else goes to the AI, whose tools (chat.py) hold the facts. The code, not the AI,
decides capacity, availability, price and the hold. The ID proof and the feedback after
checkout (idproof.py) are plain chat too. All replies are text: this bot never sends voice.

State per guest lives in repo.get_state(phone): {lang, style, bk, turn, idp, fb, ...}.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from app.core.clock import ist_today, to_ist
from app.core.language import is_language_neutral, is_opt_in, is_opt_out
from app.core.phone import mask_phone
from app.providers.llm import LLMProvider
from app.resort.chat import _more_rooms_in, PAY_LATER, BOOKING_WISH, CHANGE_ASK, GREETING, PHOTO_ASK, ROOM_OPTIONS_ASK, ROOMS_OFFER_ASK, YES_REPLY, RESTART, RESTART_MARK, ChatMixin, english_letters_only, is_ack
from app.resort.config import ResortConfig
from app.resort.db import ResortRepo
from app.resort.i18n import FIXED_LANGS, LANGS, detect_script, t
from app.resort.idproof import IdProofMixin
from app.resort.models import Booking, ChatMessage as Chat, RoomType
from app.resort.paying import apply_payment, method_label
from app.resort.payments import PaymentError, Payments
from app.whatsapp.client import WhatsAppClient, WhatsAppError
from app.whatsapp.parse import EchoMessage, InboundMessage

ANYTHING_ELSE = {t("anything_else", lg) for lg in ("en", "mn", "tn", "hn")}
# the answer to 'Is there anything else?' that needs no reply
# asking for the resort's location / map ('Location send pannunga', 'send location', 'google map', 'where is the
# resort', 'evide aanu resort'), not the 'Kindly confirm the Location' answer
MAP_ASK = re.compile(r"\b(?:location|map|maps|google ?map|pin|directions?|route)\b.*\b(?:send|share|pannunga|ayakk\w*|"
                     r"tharo|link|please|pls|venam|kodukk\w*|bhejo|bhej\w*)\b|\b(?:send|share)\b.*\b(?:location|map|pin)\b|"
                     r"\bwhere (?:is|are) (?:the |your )?(?:resort|property|place|you)\b|\bevide(?:yaa?nu| aanu)\b.*"
                     r"\b(?:resort|stalam|place)\b|\bmaps?\s*\??$|\blocation\s*\?+$", re.IGNORECASE)
NOTHING = re.compile(r"^\s*(?:no+|nope|nah|nothing(?: else)?|no thanks?|no thank you|that'?s (?:all|it)|thats (?:all|it)|"
                     r"illa|ella|onnum (?:illa|venda)|onnumilla|venda|vendaa|nahi|kuch nahi|nahi chahiye|onnum vendaa|"
                     r"edhuvum (?:illa|venam)|illai|vendam|all good|done)(?:\s+(?:sir|bro|thanks?|thank you|ok))*[\s.!]*$",
                     re.IGNORECASE)

log = logging.getLogger(__name__)

# Guests never see a booking id (owner, 2026-11-18): 'booking RB-1D9E49', '(RB-1D9E49)', 'ID: RB-1D9E49' are removed.
BOOKING_ID = re.compile(r"\s*\(?\b(?:id\s*:?\s*)?RB-[A-Z0-9]{6}\b\)?", re.IGNORECASE)
AUTO_RESUME = timedelta(hours=24)
SILENT_TYPES = ("reaction", "sticker", "unsupported", "system", "request_welcome", "edit", "revoke", "ephemeral",
                "text", "button", "interactive")  # the last three only get here when their text is empty
INDIC = re.compile(r"[ऀ-ॿ஀-௿ഀ-ൿ]")


def fmt_date(d: date) -> str:
    return f"{d.day} {d.strftime('%b')} {d.year}"



# The client wants only the booking confirmation after payment (2026-11-17), so the ID photo request is not sent then.
# The ID flow itself is kept: set True to ask again right after payment.
ASK_ID_AFTER_PAYMENT = False


def profile_name(raw: str | None) -> str:
    """The WhatsApp profile name if it looks like a name (2+ letters, no links), else ''. Used for a booking only
    when the guest gives their number without a name."""
    name = " ".join((raw or "").split())[:60]
    if sum(ch.isalpha() for ch in name) < 2 or any(x in name.lower() for x in ("http", "www.", "@")):
        return ""
    return name

PAYING_NOW = re.compile(r"\b(?:paying|paid|pay(?:ing)? now|pay cheythu|payment (?:done|made))\b|"
                        r"അടച്ചു|അടയ്ക്കാം", re.IGNORECASE)


class BookingBot(IdProofMixin, ChatMixin):
    def __init__(self, config: ResortConfig, repo: ResortRepo, whatsapp: WhatsAppClient, payments: Payments,
                 clock: Any, *, llm: Callable[[], LLMProvider] | None = None,
                 voice_in: Callable[[], Any] | None = None, debounce_s: float = 0.0, patience_s: float = 0.0,
                 followup_s: float = 0.0) -> None:
        self.followup_s = followup_s  # after a final 'ok': wait this long, then 'Is there anything else?' (0 = never)
        self.debounce_s = debounce_s  # seconds to wait for a quick second message before answering
        self.patience_s = patience_s or debounce_s  # the longer wait when the message stops mid-sentence
        self.config = config
        self.repo = repo
        self.wa = whatsapp
        self.payments = payments
        self.clock = clock
        self._quotes: dict[str, str] = {}  # phone -> note about the message the guest swipe-replied to
        self._quoted_rooms: dict[str, str] = {}  # phone -> room type id of the photo the guest swipe-replied to
        self._llm = llm
        self._voice_in = voice_in or (lambda: None)

    # ------------------------------------------------------------------ sending

    def _log(self, phone: str, text: str, source: str = "text", wamid: str = "") -> None:
        self.repo.add_chat(Chat(phone, "out", text, self.clock.now(), source, wamid))

    def _already_sent(self, phone: str, text: str) -> bool:
        """True when this exact message already went to the guest in the current conversation (client rule,
        2026-11-17: never send any message twice)."""
        rows = self.repo.list_chat(phone, limit=60)
        rows = rows[self._session_start(rows, by_time=True):]  # a day later the same answer may go again
        wanted = " ".join(text.split())
        return any(m.direction == "out" and " ".join(m.text.split()) == wanted for m in rows)

    def _text(self, phone: str, text: str, *, repeat_ok: bool = False) -> bool:
        text = english_letters_only(text)  # never Malayalam / Tamil / Hindi letters in a text (owner, 2026-11-18)
        text = BOOKING_ID.sub("", text).replace(" .", ".").strip()  # guests never see a booking id (owner, 2026-11-18)
        text = self._ease_sir(phone, text)  # 'sir' only at the start of the chat, on EVERY message (owner, 2026-11-21)
        if not repeat_ok and self._already_sent(phone, text):
            log.info("Not sending a message twice to %s", mask_phone(phone))
            return False
        try:
            wamid = self.wa.send_text(phone, text)
        except Exception as exc:  # WhatsApp error or network failure: logged, never crashes the turn, not marked sent
            log.error("Could not send to %s: %s", mask_phone(phone), exc)
            return False
        self._log(phone, text, wamid=wamid if isinstance(wamid, str) else "")
        return True

    def _note_quote(self, phone: str, context_id: str | None) -> None:
        """A swipe-reply: remember which earlier message the guest is answering, for the AI's next turn."""
        quoted = self.repo.chat_by_wamid(phone, context_id or "")
        self._quoted_rooms.pop(phone, None)
        if quoted is None:
            self._quotes.pop(phone, None)
            return
        who = "your (the assistant's) message" if quoted.direction == "out" else "their own earlier message"
        if quoted.direction == "out" and (room := self._room_of_message(quoted.text)):
            # a swipe-reply to a room photo (no caption on photos) or to its '*Room 108 - ...*' line: that room
            # (owner, 2026-11-22). 'this one venam', 'price?', 'is this free?' are about it.
            rt, num = room
            self._quoted_rooms[phone] = rt.id
            label = f"Room {num} - {rt.name}" if num else rt.name
            self._quotes[phone] = (f"[The guest swiped to reply to a photo of {label}. Their message is about THIS "
                                   "room; 'this one' means this room.]")
            return
        self._quotes[phone] = f'[The guest swiped to reply to {who}: "{quoted.text[:400]}". Answer about that.]'

    def _room_of_message(self, text: str) -> tuple[RoomType, str] | None:
        """The room a sent photo ('[photo] <ref>') or room label ('*Room 108 - Beach View Standard Rooms*') shows."""
        label = re.fullmatch(r"\*(?:Room (\w+) - )?(.+?)\*", text.strip())
        ref = text[len("[photo] "):].strip() if text.startswith("[photo] ") else ""
        if not (label or ref):
            return None
        for p in self.repo.list_properties(active_only=False):
            for rt in self.repo.list_room_types(p.id):
                if ref:
                    for num, refs in rt.number_photos.items():
                        if ref in refs:
                            return rt, num
                    if ref in rt.photo_urls:
                        return rt, ""
                elif label and label.group(2).strip() == rt.name:
                    return rt, label.group(1) or ""
        return None

    # ------------------------------------------------------------------ entry point

    def handle(self, msg: InboundMessage) -> str:
        now, phone = self.clock.now(), msg.phone
        if not self.repo.mark_event_processed(msg.id, now):
            return "duplicate"
        st = self.repo.get_state(phone)
        lang = st.get("lang", "en")
        guest = self.repo.touch_guest(phone, now, language=st.get("lang"), name=profile_name(msg.profile_name))

        if msg.type in ("image", "document") and st.get("idp"):  # a guest sending the ID photo we asked for
            return self._id_media(phone, st, msg)

        text, source = msg.text.strip(), ("button" if msg.reply_id else "text")
        if msg.type == "audio":
            try:
                self.wa.mark_read(msg.id)
            except Exception:  # best effort only
                pass
            voice = self._voice_in()
            why, result = "voice notes are switched off (no speech-to-text key)" if voice is None else "", None
            for attempt in range(2 if voice else 0):  # one retry: a download or speech-to-text hiccup (2026-11-21)
                try:
                    result = voice.process(msg.media_id)
                except Exception as exc:  # download / ffmpeg / STT crashed: the guest still gets the polite text
                    log.error("Voice note failed for %s: %s", mask_phone(phone), exc)
                    result, why = None, f"crash: {type(exc).__name__}: {str(exc)[:120]}"
                    continue
                if (result.text or "").strip():
                    break
                why = f"{getattr(result, 'problem', None) or 'empty'}: {(getattr(result, 'detail', '') or '')[:120]}".strip(": ")
                if getattr(result, "problem", None) in ("too_long", "unclear"):
                    break  # the same audio gives the same answer: no retry
            if result is None or not (result.text or "").strip():
                log.error("Voice note not understood for %s: %s", mask_phone(phone), why)
                # the reason is kept in the chat (dashboard) so a failure can be checked, never guessed
                self.repo.add_chat(Chat(phone, "in", f"[voice note: not understood ({why})]", now, "voice"))
                self._text(phone, t("voice_problem", lang), repeat_ok=True)  # every failed note gets an answer
                return "voice_problem"
            text, source = result.text, "voice"
        elif msg.type == "image" and msg.media_id:  # a payment screenshot or a photo: the AI looks at it
            return self._image_turn(phone, st, msg, now)
        elif not msg.is_text:
            self.repo.add_chat(Chat(phone, "in", f"[{msg.type}]", now, "text"))
            # reactions, stickers, edits / deletes / polls (Meta's 'unsupported'), system events and empty texts get no
            # reply; location, contacts, video ... get one short "text or voice only" line
            if msg.type not in SILENT_TYPES:
                self._text(phone, t("unsupported", lang))
            return "unsupported"
        in_id = self.repo.add_chat(Chat(phone, "in", text, now, source, msg.id))
        self._note_quote(phone, msg.context_id)

        paused, since = self.repo.is_paused(phone)
        if paused:
            if since is not None and now - since >= AUTO_RESUME:
                self.repo.set_paused(phone, False)
            else:
                return "paused"
        try:
            self.wa.mark_read(msg.id)
        except Exception:
            pass
        # one guest's messages are answered one after another (live chat 2026-11-21: '4 people, one room' sent while
        # the photos of the first answer were still going cut them off and sent a room label twice). The message is
        # already saved, so an earlier turn still waiting for a second message sees it and steps aside.
        with self._guest_lock(phone):
            st = self.repo.get_state(phone)  # fresh: the earlier message may have changed it
            return self._dispatch(phone, st, text, None if source != "button" else msg.reply_id, source, guest.name,
                                  in_id)

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    @classmethod
    def _guest_lock(cls, phone: str) -> threading.RLock:
        with cls._locks_guard:
            return cls._locks.setdefault(phone, threading.RLock())

    # Client, 2026-11-17: images are not read by the AI for now and get no reply. Set True to bring it back.
    READ_IMAGES = False

    def _image_turn(self, phone: str, st: dict[str, Any], msg, now) -> str:
        """An image from a guest with no ID request open. It is saved in the chat and gets NO reply (client rule).
        Razorpay is still asked about unpaid holds: a payment that really went through gets its normal confirmation."""
        import base64
        caption = (msg.text or "").strip()
        self.repo.add_chat(Chat(phone, "in", f"[image] {caption}".strip(), now, "text"))
        paused, _ = self.repo.is_paused(phone)
        if paused:
            return "paused"
        try:
            self.wa.mark_read(msg.id)
        except Exception:
            pass
        if not self.READ_IMAGES:
            try:
                self.check_payments()
            except Exception as exc:
                log.warning("Payment check failed: %s", exc)
            return "image_ignored"
        try:
            data, mime = self.wa.download_media(msg.media_id, max_bytes=5_000_000)
        except Exception as exc:
            log.warning("Image download failed for %s: %s", mask_phone(phone), exc)
            self._text(phone, t("unsupported", st.get("lang", "en")))
            return "unsupported"
        mime = (mime or "image/jpeg").split(";")[0].strip().lower()
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            self._text(phone, t("unsupported", st.get("lang", "en")))
            return "unsupported"
        try:
            self.check_payments()
        except Exception as exc:
            log.warning("Payment check failed: %s", exc)
        st.setdefault("lang", "en")
        text = f"[The guest sent an image{': ' + caption if caption else ''}]"
        return self._converse(phone, st, text, 0, images=[(mime, base64.b64encode(data).decode())])

    # ------------------------------------------------------------------ routing

    def _save(self, phone: str, st: dict[str, Any]) -> None:
        self.repo.set_state(phone, st)

    def _brand(self) -> str:
        return self.config.display_name

    def _today(self) -> date:
        return ist_today(self.clock)

    def _unpaid_ack(self, phone: str, st: dict[str, Any], text: str) -> str | None:
        """'ok' / 'thanks' / 'ok paying now' while the advance is unpaid (live chat 2026-11-22: 'see you soon' before
        any payment). Short nudge, or the link when it is not the message just above; 'paying now' only waits.
        None when no payment is waiting or the message is something else."""
        if "?" in text:
            return None
        paying = bool(PAYING_NOW.search(text)) and len(text.split()) <= 6 and not PAY_LATER.search(text)
        if not (is_ack(text) or paying):
            return None
        now = self.clock.now()
        held = next((b for b in self.repo.list_bookings(phone=phone, limit=3) if b.status == "hold"
                     and b.hold_expires_at and b.hold_expires_at > now and b.payment_link_url), None)
        if held is None:
            return None
        lang = st.get("lang", "en")
        if paying:  # check Razorpay, never nag with the link
            if not self.check_payments():
                self._text(phone, t("pay_wait", lang), repeat_ok=True)
            return "hold"
        just_sent = held.payment_link_url in (self.repo.last_outbound_text(phone) or "")
        self._text(phone, t("pay_nudge", lang) if just_sent else t("hold_pay", lang, url=held.payment_link_url),
                   repeat_ok=True)
        return "hold"

    def _dispatch(self, phone: str, st: dict[str, Any], text: str, rid: str | None, source: str,
                  guest_name: str, in_id: int = 0) -> str:
        typed = rid is None
        if typed:
            if st.get("opted_out") and is_opt_in(text):
                st["opted_out"] = False
                self._save(phone, st)
                guest = self.repo.touch_guest(phone, self.clock.now())
                guest.offers_consent = True
                self.repo.upsert_guest(guest)
                self._text(phone, t("opt_in", st.get("lang", "en")))
                return "opt_in"
            if is_opt_out(text) and not st.get("opted_out"):
                st["opted_out"] = True
                self._save(phone, st)
                guest = self.repo.touch_guest(phone, self.clock.now())
                guest.offers_consent = False
                self.repo.upsert_guest(guest)
                self._text(phone, t("opt_out", st.get("lang", "en")))
                return "opt_out"
            if RESTART.match(text):  # 'restart': a fresh conversation (owner, 2026-11-18). Bookings and records stay.
                # the language starts fresh too (owner, 2026-11-21: 'yes' after restart got Manglish from the old
                # chat): English until the guest writes in another language
                keep = {k: st[k] for k in ("opted_out",) if k in st}
                st.clear()
                st.update(keep)
                self._save(phone, st)
                self._text(phone, t("greet", st.get("lang", "en")), repeat_ok=True)
                self._log(phone, RESTART_MARK, "system")  # the conversation starts after this line (a 'hi' greets)
                return "restart"
            if INDIC.search(text):  # a script gives the language away at once
                st["lang"] = st["gs"] = detect_script(text, st.get("lang", "en"))
            elif not is_language_neutral(text):
                st["gs"] = ""  # English letters (English or Manglish): "ok" and "hi" do not change this
            # the reply language is read BEFORE any fixed step answers (location question, form, 'ok', photos...),
            # so a code path never answers a Manglish / Tanglish / Hinglish guest in English (Step 3, 2026-11-18)
            if not is_ack(text):  # 'ok' / 'thanks a lot' keep the language
                # a 1-2 word message ('Alleppey') is read with what the guest wrote since our last reply, as the AI
                # path does ('avde engane und' + 'Alleppey' is Manglish)
                short = len(text.split()) <= 2
                self._language_note(st, self._since_last_reply(phone) if short else text)
        st.setdefault("lang", "en")
        if st["lang"] not in FIXED_LANGS:
            st["lang"] = "en"

        if not typed:  # a stale button from an old message
            return "ignored"
        if st.get("idp") and (outcome := self._id_step(phone, st, text)):
            return outcome
        if st.get("fb") and (outcome := self._fb_step(phone, st, text)):
            return outcome
        if (self.repo.last_outbound_text(phone) or "") in ANYTHING_ELSE and (is_ack(text) or NOTHING.match(text)):
            # 'Is there anything else?' -> 'no thanks': the AI says a polite goodbye in its own words (owner,
            # 2026-11-21: reply to whatever they say, politely)
            return "answered" if self.answer_question(phone, st, text) == "answered" else "quiet"
        if st.get("suggest_rooms") and (is_ack(text) or YES_REPLY.match(text)):
            return self._converse(phone, st, text, in_id)  # 'ok' to 'Shall I go ahead with 5 rooms?': checked in code
        last_out = self.repo.last_outbound_text(phone) or ""
        steps = {t(k, lg) for k in ("contact_again", "fill_form", "ask_contact", "form") for lg in ("en", "mn", "tn", "hn")}
        if (is_ack(text) or YES_REPLY.match(text)) and "?" in last_out and last_out != self.next_step(phone, st) \
                and last_out not in ANYTHING_ELSE and last_out not in steps:
            # 'Yes' / 'Ok' answering the bot's own question ('Shall I book 3 rooms?') is an ANSWER: it is acted on,
            # never silence (live chat 2026-11-20: 'Yes' and 'Ok' got nothing)
            if (m := ROOMS_OFFER_ASK.search(last_out)) and (st.get("bk") or {}).get("check_in"):
                st["suggest_rooms"] = int(m.group(1))  # 'Shall I book 3 rooms?' -> 'Yes': checked in code, 3 rooms
            return self._converse(phone, st, text, in_id)
        if outcome := self._unpaid_ack(phone, st, text):
            return outcome
        if is_ack(text) and "?" not in text:  # "ok" mid-chat ("?" is a question, not an ok) (owner, 2026-11-18): carry on to the step still open, else stay quiet
            # 'ok', 'thanks a lot', 'adipoli', 'ok will see': the AI replies politely in its own words, like an agent
            # (owner, 2026-11-21: no fixed replies to fixed words); then the code adds the booking step still open
            step = self.next_step(phone, st)
            spoke = self.answer_question(phone, st, text) == "answered"
            st = self.repo.get_state(phone)
            if step:
                lang = st.get("lang", "en")
                if step == (self.repo.last_outbound_text(phone) or "") or spoke:
                    # just above, or right after the AI's words: the short pointer, never the long step twice
                    step = t("fill_form", lang) if step == t("form", lang) else t("contact_again", lang) \
                        if step == t("ask_contact", lang) else step
                if step != (self.repo.last_outbound_text(phone) or ""):
                    st["ok_reminded"] = step
                    self._save(phone, st)
                    self._text(phone, step, repeat_ok=True)
                return "next_step"
            if spoke:
                return "answered"
            if not self._bot_asked_something(phone, st):
                return self._final_ok(phone, st, in_id)
        if outcome := self._room_choice_now(phone, st, text, in_id):
            return outcome  # 'I only need cottage' / 'room 103 venam': that room, with the dates already known
        if outcome := self._hold_now(phone, st, text, in_id):
            return outcome  # 'hold the room' / 'pay tomorrow': pay the advance now, one line and the link
        if outcome := self._price_now(phone, st, text):
            return outcome  # the price of the checked stay, from the code: the dates are never asked again
        # 'one more room' / '1 room koode' ADDS a room (checked in code); it is not a request for other room photos
        room_options_now = bool(st.get("checked") and ROOM_OPTIONS_ASK.search(text) and not _more_rooms_in(text))
        if not room_options_now and self._other_question(text) and (PHOTO_ASK.search(text) or BOOKING_WISH.search(text)
                                            or self._named_place(text) is not None):
            # 'Do you have rooms with a sea view?': the AI answers it first, then the fixed step below goes (owner,
            # 2026-11-20: answer like an agent). The question is never answered twice.
            answered = self.answer_question(phone, st, text) == "answered"
            st = self.repo.get_state(phone)
        else:
            answered = False
        if MAP_ASK.search(text) and (outcome := self._send_map(phone, st, text)):
            return outcome
        if (PHOTO_ASK.search(text) or room_options_now) \
                and (outcome := self._photos_now(phone, st, text)):
            return outcome  # photos at any stage (owner, 2026-11-19): the free room by priority, else the top one
        if PHOTO_ASK.search(text) and not st.get("checked"):
            # photos come with the glimpse once rooms are found (owner's flow); before that, the next step
            step = self._open_step(phone, st)
            if step is None:
                return self._converse(phone, st, text, in_id)  # nothing open: the AI answers about the photos
            if step == (self.repo.last_outbound_text(phone) or "") and step == t("form", st.get("lang", "en")):
                step = t("fill_form", st.get("lang", "en"))  # the form is just above: point to it, never silence
            self._text(phone, step, repeat_ok=True)
            return "next_step"
        if GREETING.match(text) and not st.get("bk") and self._places() and not self._chatting_now(phone):
            self._save(phone, st)
            return self._greet(phone, st, text)
        if GREETING.match(text) and self._chatting_now(phone):
            # a mid-chat 'hi' gets the owner's greeting (2026-11-18), never an AI guess (a paid guest was once told
            # "Payment complete aayal booking confirm aakum"). The conversation is remembered; nothing restarts.
            # every new 'hi' gets an answer, even right after the last one (a silent bot looks broken, 2026-11-18).
            # Owner, 2026-11-22: not 'How may I assist you?' again: 'Hi' with what we already know, then the open step
            self._save(phone, st)
            self._hello_back(phone, st)
            return "greeting"
        if CHANGE_ASK.search(text) and (live := [b for b in self.repo.list_bookings(phone=phone, limit=5)
                                                 if b.status in ("paid", "checked_in", "hold")]):
            # a guest wants to change or cancel a booking: the owner hears at once (the AI points them to the owner)
            b = live[0]
            self.alert_staff(f"Guest {b.guest_name or ''} {phone} wants to change/cancel booking {b.id} "
                             f"({fmt_date(b.check_in)} to {fmt_date(b.check_out)}): \"{text[:200]}\"", b.property_id)
        if outcome := self.booking_wish(phone, st, text):
            return outcome
        if outcome := self._location_answer(phone, st, text):
            return outcome
        if answered:
            # the AI answered the question; the booking still moves on: the open step, else location / form
            live = any(b.status in ("paid", "checked_in", "hold") and b.check_out >= self._today()
                       for b in self.repo.list_bookings(phone=phone, limit=10))
            if BOOKING_WISH.search(text) and not (st.get("bk") or {}).get("check_in") and not live:
                lang = st.get("lang", "en")
                if prop := self._known_place(st) or self._named_place(text):
                    st["form_place"] = prop.id
                    self._save(phone, st)
                    self._text(phone, self._missing_form(lang, self._stay_known(phone, st)), repeat_ok=True)
                else:
                    self._text(phone, t("ask_location", lang, towns=self._towns_text()), repeat_ok=True)
            return "answered"  # the AI already answered this message above
        return self._converse(phone, st, text, in_id)

    # ------------------------------------------------------------------ staff handoff

    def staff_numbers(self, property_id: str | None = None) -> list[str]:
        numbers = list(self.config.staff_numbers)
        props = [self.repo.get_property(property_id)] if property_id else self.repo.list_properties()
        for p in props:
            if p:
                numbers += p.staff_numbers
        return list(dict.fromkeys(numbers))

    def alert_staff(self, text: str, property_id: str | None = None) -> None:
        for number in self.staff_numbers(property_id):
            try:
                self.wa.send_text(number, text)
            except Exception as exc:  # outside their 24 h window (needs a template at go-live) or a network error
                log.warning("Staff alert to %s failed: %s", mask_phone(number), exc)

    def handoff(self, phone: str, st: dict[str, Any], reason: str, *, notify_guest: bool = True) -> str:
        now = self.clock.now()
        guest = self.repo.get_guest(phone)
        recent = self.repo.list_chat(phone, limit=6)
        lines = [f"{'Guest' if m.direction == 'in' else 'Bot'}: {m.text[:120]}" for m in recent]
        summary = (f"Guest needs the team: {reason}\n{guest.name if guest and guest.name else 'Unknown'} {phone}\n"
                   f"Language: {st.get('lang', 'en')}\n" + "\n".join(lines))
        # the bot never pauses itself (owner, 2026-11-20: a guest's 'Hi' after a failed payment link got silence for
        # 24 h): staff get the summary and the bot keeps answering. Only staff pause a chat (dashboard / their reply).
        if notify_guest:
            self._text(phone, t("handoff", st.get("lang", "en")))
        self.alert_staff(summary, st.get("property_id"))
        return "handoff"

    def human_replied(self, echo: EchoMessage) -> str:
        """Staff typed a reply to this guest in the WhatsApp Business app: the bot steps back.
        Every new staff message restarts the 24 h auto-resume timer."""
        now = self.clock.now()
        if not self.repo.mark_event_processed(echo.id, now):
            return "duplicate"
        text = echo.text.strip() or f"[{echo.type}]"
        for m in self.repo.list_chat(echo.phone, limit=5):  # a copy of something the bot sent is not a human
            if m.direction == "out" and m.source != "staff" and m.text.strip() == text and now - m.at < timedelta(minutes=2):
                return "bot_echo"
        self.repo.add_chat(Chat(echo.phone, "out", text, now, "staff"))
        self.repo.set_paused(echo.phone, True, now)
        return "paused_by_human"

    # ------------------------------------------------------------------ payment events and sweeps

    def _notify_guest(self, booking: Booking, text: str, template_key: str, params: list[str]) -> bool:
        """Text inside the 24 h window, else the approved template. True only when the plain text went out."""
        phone = booking.guest_phone
        text = english_letters_only(text)  # never Malayalam / Tamil / Hindi letters in a text (owner, 2026-11-18)
        text = BOOKING_ID.sub("", text).replace(" .", ".").strip()  # guests never see a booking id (owner, 2026-11-18)
        try:
            self.wa.send_text(phone, text)
            self._log(phone, text, "system")
            return True
        except Exception as exc:
            if getattr(exc, "code", None) != 131047:  # 131047 = outside the 24 h window
                log.error("Could not notify %s: %s", mask_phone(phone), exc)
                return False
        name = getattr(self.config.templates, template_key, "") or os.environ.get(f"RESORT_TPL_{template_key.upper()}", "")
        if not name:
            log.warning("Guest %s is outside the 24 h window and no %s template is set", mask_phone(phone), template_key)
            return False
        try:
            self.wa.send_template(phone, name, booking.language if booking.language in LANGS else "en", params)
            self._log(phone, f"[template {name}] " + text, "system")
        except Exception as exc:
            log.error("Template %s to %s failed: %s", name, mask_phone(phone), exc)
        return False

    def _params(self, b: Booking) -> list[str]:
        prop = self.repo.get_property(b.property_id)
        return [b.id, prop.name if prop else "", fmt_date(b.check_in), fmt_date(b.check_out), str(b.rooms)]

    def mark_paid(self, *, booking_id: str = "", link_id: str = "", payment_id: str = "") -> str:
        """Razorpay says the link is paid. Idempotent. Returns paid | already_paid | refund_needed | unknown."""
        b = (self.repo.get_booking(booking_id) if booking_id else None) or (
            self.repo.find_booking_by_link(link_id) if link_id else None)
        if b is None:
            return "unknown"
        if b.status == "paid":
            return "already_paid"
        now = self.clock.now()
        parts = self.repo.list_group(b)  # one link pays every part of a stay over two room types
        b = parts[0] if b.group_id else b
        if b.status == "paid":
            return "already_paid"
        st = self.repo.get_state(b.guest_phone)
        if st.pop("room_pref", None):  # the picked room type was for this stay; the next enquiry starts fresh
            self._save(b.guest_phone, st)
        gone = False
        for x in parts:
            xrt = self.repo.get_room_type(x.room_type_id)
            if x.status in ("expired", "cancelled") and xrt is not None \
                    and self.repo.free_rooms(xrt, x.check_in, x.check_out, now) < x.rooms:
                gone = True
        if gone:
            b.notes = (b.notes + " PAID AFTER EXPIRY, ROOMS GONE: REFUND NEEDED").strip()
            b.razorpay_payment_id = payment_id or b.razorpay_payment_id
            self.repo.save_booking(b)
            self.alert_staff(f"REFUND NEEDED: {b.id} was paid after the hold expired and the rooms are gone "
                             f"({b.guest_name} {b.guest_phone}, Rs {b.advance}).", b.property_id)
            return "refund_needed"
        for x in parts:
            if x.status == "paid":
                continue
            x.razorpay_payment_id = payment_id or x.razorpay_payment_id
            apply_payment(x, method="online", amount=x.advance if x.advance >= 1 else x.total,
                          ref=payment_id or link_id or x.payment_link_id or f"online-{x.id}", now=now)
            self.repo.save_booking(x)
        self.close_old_holds(b.guest_phone, {x.id for x in parts})  # no reminder may follow about an old enquiry
        self.announce_paid(self.repo.get_booking(b.id) or b)
        return "paid"

    def reissue_link(self, b: Booking, *, discount: int = 0, by: str = "") -> str | None:
        """Give an unpaid booking a fresh payment link (optionally after the owner took `discount` rupees off the total)
        and send it to the guest. The old link is closed so it can never be paid at the old amount. Returns an error
        text for staff, or None."""
        if b.status != "hold" or b.paid_amount:
            return "Only a booking that is waiting for payment can get a new link or a discount"
        if b.group_id:
            return "This stay is split over two room types; please send the guest a link yourself"
        prop, rt = self.repo.get_property(b.property_id), self.repo.get_room_type(b.room_type_id)
        now = self.clock.now()
        total, advance = b.total, b.advance
        if discount:
            if not 0 < discount < b.total:
                return "The discount must be more than 0 and less than the total"
            total = b.total - discount
            advance = round(total * (prop.advance_pct if prop else 50) / 100)
        hold = timedelta(minutes=self.config.hold_minutes)
        try:
            link = self.payments.create_payment_link(
                advance, b.id, b.guest_name, b.guest_phone,
                f"{prop.name if prop else ''}: {rt.name if rt else ''} x {b.rooms}, {fmt_date(b.check_in)} to {fmt_date(b.check_out)}",
                int((now + max(hold, timedelta(minutes=16))).timestamp()))
        except PaymentError as exc:
            return f"Could not create the payment link: {exc}"
        if b.payment_link_id:
            try:
                self.payments.cancel_link(b.payment_link_id)
            except PaymentError as exc:
                log.warning("Could not close the old link of %s: %s", b.id, exc)
        if discount:
            stamp = to_ist(now).strftime("%d %b %H:%M")
            b.notes = (b.notes + f"\n[{stamp} {by}] Discount Rs {discount}: total Rs {b.total} -> Rs {total}").strip()
            b.total, b.advance = total, advance
        b.payment_link_id, b.payment_link_url = link.id, link.url
        b.hold_expires_at = now + hold
        self.repo.save_booking(b)
        lang = b.language if b.language in FIXED_LANGS else "en"
        minutes = max(1, int(hold.total_seconds() // 60))
        text = t("pay", lang, id=b.id, minutes=minutes, advance=f"{b.advance:,}", url=link.url)
        if discount:
            text = t("discount", lang, total=f"{b.total:,}", advance=f"{b.advance:,}") + "\n\n" + text
        if not self._notify_guest(b, text, "pay", self._params(b)):
            return "The link is ready, but the guest could not be messaged on WhatsApp (outside the 24 hour window)"
        return None

    def announce_paid(self, b: Booking) -> None:
        """Tell the guest their booking is confirmed and alert the staff (online or desk payment)."""
        prop = self.repo.get_property(b.property_id)
        rt = self.repo.get_room_type(b.room_type_id)
        # The client's confirmation, then the cancellation policy, then the stay details (owner, 2026-11-18).
        delivered = self._notify_guest(b, self._confirmed_text(b, prop, rt), "paid", self._params(b))
        if delivered and prop:
            for extra in (prop.policies, prop.stay_info):
                if extra and extra.strip():
                    self._text(b.guest_phone, extra.strip())
        self.end_conversation(b.guest_phone)  # a confirmed booking closes the chat: the next message starts fresh
        try:  # ask for the ID proof once (only inside the 24 h window; the day-before reminder covers the rest)
            if delivered and ASK_ID_AFTER_PAYMENT:
                self.start_id_request(b)
        except Exception:
            log.exception("ID request for %s failed", b.id)
        parts = self.repo.list_group(b)
        names = []
        for x in parts:
            xrt = self.repo.get_room_type(x.room_type_id)
            names.append(f"{xrt.name if xrt else ''} x {x.rooms}")
        self.alert_staff(f"New booking {' + '.join(x.id for x in parts)} PAID: {prop.name if prop else ''}, "
                         f"{' + '.join(names)}, {fmt_date(b.check_in)} to {fmt_date(b.check_out)}, "
                         f"{sum(x.adults for x in parts)}A {sum(x.children for x in parts)}C. "
                         f"{b.guest_name} {b.guest_phone}. Paid Rs {sum(x.paid_amount for x in parts)} of Rs "
                         f"{sum(x.total for x in parts)} "
                         f"({method_label(b.payment_method)}).", b.property_id)

    def _send_map(self, phone: str, st: dict[str, Any], text: str) -> str | None:
        """'Location send pannunga' / 'map?' / 'where is the resort': the resort's Google Maps link (the chosen resort,
        the one named, else the only open one). None when no link is set (the AI answers)."""
        bk = st.get("bk") or {}
        prop = self.repo.get_property(bk.get("property_id") or st.get("form_place") or "") if (
            bk.get("property_id") or st.get("form_place")) else None
        if prop is None:
            places = self._places()
            prop = self._named_place(text) or (places[0] if len(places) == 1 else None)
        if prop is None or not prop.map_url:
            return None
        self._save(phone, st)  # keep the language read from this message
        self._text(phone, f"📍 {prop.name}\n{prop.map_url}", repeat_ok=True)
        if step := self.next_step(phone, st):
            if step != (self.repo.last_outbound_text(phone) or ""):
                self._text(phone, step, repeat_ok=True)
        return "map"

    def _final_ok(self, phone: str, st: dict[str, Any], in_id: int) -> str:
        """A last 'ok' with nothing open (owner, 2026-11-19): if the guest writes nothing more for followup_s seconds,
        'Is there anything else?' once per conversation."""
        if not self.followup_s or not in_id:
            return "quiet"
        rows = self.repo.list_chat(phone, limit=120)
        if any(m.direction == "out" and m.text in ANYTHING_ELSE for m in rows[self._session_start(rows):]):
            return "quiet"
        time.sleep(self.followup_s)
        if self.repo.last_chat_id(phone, "in") != in_id:
            return "superseded"  # they wrote again: that message is answered instead
        self._text(phone, t("anything_else", st.get("lang", "en")))
        return "anything_else"

    def end_conversation(self, phone: str) -> None:
        """Close the current chat (booking confirmed or cancelled, owner 2026-11-18): the guest's next message starts a
        fresh conversation. The booking stays in their records, which the AI always sees."""
        st = self.repo.get_state(phone)
        for key in ("bk", "checked", "form_place", "intro_sent", "last_summary"):  # the finished booking's steps only
            st.pop(key, None)
        self._save(phone, st)
        self._log(phone, RESTART_MARK, "system")

    def payment_failed(self, *, booking_id: str = "", link_id: str = "", payment_id: str = "") -> str:
        b = (self.repo.get_booking(booking_id) if booking_id else None) or (
            self.repo.find_booking_by_link(link_id) if link_id else None)
        if b is None or b.status == "paid":
            return "ignored"
        mark = f"[failed:{payment_id}]"
        if payment_id:
            if mark in b.notes:  # the webhook and our own check both saw it: the guest hears once
                return "ignored"
            fresh = self.repo.get_booking(b.id) or b
            fresh.notes = (fresh.notes + " " + mark).strip()
            self.repo.save_booking(fresh)
        text = t("failed", b.language, id=b.id)
        if b.status == "hold" and b.payment_link_url:
            text += "\n" + t("retry_link", b.language, url=b.payment_link_url)
        self._notify_guest(b, text, "failed", self._params(b))
        return "notified"

    def payment_expired(self, *, booking_id: str = "", link_id: str = "") -> str:
        b = (self.repo.get_booking(booking_id) if booking_id else None) or (
            self.repo.find_booking_by_link(link_id) if link_id else None)
        if b is None or b.status != "hold":
            return "ignored"
        for x in self.repo.list_group(b):  # the rooms go back on sale; the guest is not chased
            if x.status == "hold":
                x.status = "expired"
                self.repo.save_booking(x)
        return "expired"

    def _bot_asked_something(self, phone: str, st: dict[str, Any]) -> bool:
        """True when our last message to this guest waits for an answer: it has a question in it, a booking summary
        is waiting for their confirmation, or we have not written to them yet."""
        if (st.get("bk") or {}).get("chosen"):
            return True
        last = self.repo.last_outbound_text(phone)
        return last is None or "?" in last

    def check_payments(self) -> int:
        """Ask the payment provider about every unpaid hold. A paid link is confirmed exactly as the webhook would."""
        link_status = getattr(self.payments, "link_status", None)
        if link_status is None:
            return 0
        found = 0
        for b in self.repo.list_bookings(status="hold", limit=200):
            if not b.payment_link_id or (b.group_id and b.group_id != b.id):
                continue  # the second part of a two-room-type stay shares the first part's link
            try:
                status = link_status(b.payment_link_id)
            except Exception as exc:  # the provider being slow must never stop the sweep
                log.warning("Payment check for %s failed: %s", b.id, exc)
                continue
            if status.get("paid"):
                if self.mark_paid(booking_id=b.id, payment_id=status.get("payment_id", "")) == "paid":
                    found += 1
            elif status.get("failed_payment_id"):  # an attempt failed and the link is still unpaid: tell the guest once
                self.payment_failed(booking_id=b.id, payment_id=status["failed_payment_id"])
        return found

    def sweep(self, now: datetime | None = None) -> dict[str, int]:
        """Run every minute or so: confirm payments Razorpay has but we missed, expire unpaid holds (and tell the
        guests), nudge holds about to expire."""
        now = now or self.clock.now()
        found = self.check_payments()
        expired = self.repo.expire_holds(now)  # released quietly: no "expired" message to the guest
        nudged = 0
        for b in self.repo.list_bookings(status="hold", limit=200):
            if (b.hold_expires_at and b.payment_link_url and "[reminded]" not in b.notes
                    and not (b.group_id and b.group_id != b.id)
                    and timedelta(0) < b.hold_expires_at - now <= timedelta(minutes=5)):
                if any(o.created_at > b.created_at and o.id != b.id and o.group_id != b.id
                       for o in self.repo.list_bookings(phone=b.guest_phone, limit=20)):
                    self.close_old_holds(b.guest_phone, {o.id for o in self.repo.list_bookings(
                        phone=b.guest_phone, limit=20) if o.created_at > b.created_at})
                    continue  # the guest booked again since: this old enquiry is closed, never reminded
                b.notes = (b.notes + " [reminded]").strip()
                self.repo.save_booking(b)
                mins = max(1, int((b.hold_expires_at - now).total_seconds() // 60))
                self._notify_guest(b, t("pending", b.language, id=b.id, minutes=mins, url=b.payment_link_url),
                                   "pending", self._params(b))
                nudged += 1
        out = {"expired": len(expired), "reminded": nudged}
        if found:
            out["paid_found"] = found
        try:
            if reminders := self.remind_ids(now):
                out["id_reminders"] = reminders
            if purged := self._purge_id_proofs(now):
                out["id_purged"] = purged
        except Exception:
            log.exception("ID reminder / purge failed")
        return out

    def _purge_id_proofs(self, now: datetime) -> int:
        """Once a day: delete ID photos whose delete_after date has passed."""
        today = to_ist(now).date()
        state = self.repo.get_state("__system__")
        if state.get("id_purge_day") == today.isoformat():
            return 0
        state["id_purge_day"] = today.isoformat()
        self.repo.set_state("__system__", state)
        count = self.repo.delete_expired_id_proofs(today)
        if count:
            log.info("Deleted %d expired ID proof(s)", count)
        return count
