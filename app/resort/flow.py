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
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from app.core.clock import ist_today, to_ist
from app.core.language import is_opt_in, is_opt_out
from app.core.phone import mask_phone
from app.providers.llm import LLMProvider
from app.resort.chat import GREETING, ChatMixin, is_ack
from app.resort.config import ResortConfig
from app.resort.db import ResortRepo
from app.resort.i18n import FIXED_LANGS, LANGS, detect_script, t
from app.resort.idproof import IdProofMixin
from app.resort.models import Booking, ChatMessage as Chat
from app.resort.paying import apply_payment, method_label
from app.resort.payments import Payments
from app.whatsapp.client import WhatsAppClient, WhatsAppError
from app.whatsapp.parse import InboundMessage

log = logging.getLogger(__name__)

AUTO_RESUME = timedelta(hours=24)
INDIC = re.compile(r"[ऀ-ॿ஀-௿ഀ-ൿ]")


def fmt_date(d: date) -> str:
    return f"{d.day} {d.strftime('%b')} {d.year}"


class BookingBot(IdProofMixin, ChatMixin):
    def __init__(self, config: ResortConfig, repo: ResortRepo, whatsapp: WhatsAppClient, payments: Payments,
                 clock: Any, *, llm: Callable[[], LLMProvider] | None = None,
                 voice_in: Callable[[], Any] | None = None, debounce_s: float = 0.0) -> None:
        self.debounce_s = debounce_s  # seconds to wait for a quick second message before answering
        self.config = config
        self.repo = repo
        self.wa = whatsapp
        self.payments = payments
        self.clock = clock
        self._llm = llm
        self._voice_in = voice_in or (lambda: None)

    # ------------------------------------------------------------------ sending

    def _log(self, phone: str, text: str, source: str = "text") -> None:
        self.repo.add_chat(Chat(phone, "out", text, self.clock.now(), source))

    def _text(self, phone: str, text: str) -> bool:
        try:
            self.wa.send_text(phone, text)
        except WhatsAppError as exc:
            log.error("Could not send to %s: %s", mask_phone(phone), exc)
            return False
        self._log(phone, text)
        return True

    # ------------------------------------------------------------------ entry point

    def handle(self, msg: InboundMessage) -> str:
        now, phone = self.clock.now(), msg.phone
        if not self.repo.mark_event_processed(msg.id, now):
            return "duplicate"
        st = self.repo.get_state(phone)
        lang = st.get("lang", "en")
        guest = self.repo.touch_guest(phone, now, language=st.get("lang"))

        if msg.type in ("image", "document") and st.get("idp"):  # a guest sending the ID photo we asked for
            return self._id_media(phone, st, msg)

        text, source = msg.text.strip(), ("button" if msg.reply_id else "text")
        if msg.type == "audio":
            try:
                self.wa.mark_read(msg.id)
            except Exception:  # best effort only
                pass
            voice = self._voice_in()
            result = voice.process(msg.media_id) if voice else None
            if result is None or result.text is None:
                self.repo.add_chat(Chat(phone, "in", "[voice note: not understood]", now, "voice"))
                self._text(phone, t("voice_problem", lang))
                return "voice_problem"
            text, source = result.text, "voice"
        elif not msg.is_text:
            self.repo.add_chat(Chat(phone, "in", f"[{msg.type}]", now, "text"))
            if msg.type not in ("reaction", "sticker"):
                self._text(phone, t("unsupported", lang))
            return "unsupported"
        in_id = self.repo.add_chat(Chat(phone, "in", text, now, source))

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
        return self._dispatch(phone, st, text, None if source != "button" else msg.reply_id, source, guest.name, in_id)

    # ------------------------------------------------------------------ routing

    def _save(self, phone: str, st: dict[str, Any]) -> None:
        self.repo.set_state(phone, st)

    def _brand(self) -> str:
        return self.config.display_name

    def _today(self) -> date:
        return ist_today(self.clock)

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
            if INDIC.search(text):  # a script gives the language away at once
                st["lang"] = detect_script(text, st.get("lang", "en"))
        st.setdefault("lang", "en")
        if st["lang"] not in FIXED_LANGS:
            st["lang"] = "en"

        if not typed:  # a stale button from an old message
            return "ignored"
        if st.get("idp") and (outcome := self._id_step(phone, st, text)):
            return outcome
        if st.get("fb") and (outcome := self._fb_step(phone, st, text)):
            return outcome
        if is_ack(text) and not self._bot_asked_something(phone, st):
            return "quiet"  # "ok" / "thanks" after a statement: no reply, the guest will write if they want more
        if GREETING.match(text) and not st.get("bk") and self._places():
            self._save(phone, st)
            return self._greet(phone, st, text)
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
            except WhatsAppError as exc:  # outside their 24 h window: needs a template at go-live
                log.warning("Staff alert to %s failed: %s", mask_phone(number), exc)

    def handoff(self, phone: str, st: dict[str, Any], reason: str, *, notify_guest: bool = True) -> str:
        now = self.clock.now()
        guest = self.repo.get_guest(phone)
        recent = self.repo.list_chat(phone, limit=6)
        lines = [f"{'Guest' if m.direction == 'in' else 'Bot'}: {m.text[:120]}" for m in recent]
        summary = (f"Guest needs the team: {reason}\n{guest.name if guest and guest.name else 'Unknown'} {phone}\n"
                   f"Language: {st.get('lang', 'en')}\n" + "\n".join(lines))
        self.repo.set_paused(phone, True, now)
        if notify_guest:
            self._text(phone, t("handoff", st.get("lang", "en")))
        self.alert_staff(summary, st.get("property_id"))
        return "handoff"

    # ------------------------------------------------------------------ payment events and sweeps

    def _notify_guest(self, booking: Booking, text: str, template_key: str, params: list[str]) -> bool:
        """Text inside the 24 h window, else the approved template. True only when the plain text went out."""
        phone = booking.guest_phone
        try:
            self.wa.send_text(phone, text)
            self._log(phone, text, "system")
            return True
        except WhatsAppError as exc:
            if exc.code != 131047:  # 131047 = outside the 24 h window
                log.error("Could not notify %s: %s", mask_phone(phone), exc)
                return False
        name = getattr(self.config.templates, template_key, "") or os.environ.get(f"RESORT_TPL_{template_key.upper()}", "")
        if not name:
            log.warning("Guest %s is outside the 24 h window and no %s template is set", mask_phone(phone), template_key)
            return False
        try:
            self.wa.send_template(phone, name, booking.language if booking.language in LANGS else "en", params)
            self._log(phone, f"[template {name}] " + text, "system")
        except WhatsAppError as exc:
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
        rt = self.repo.get_room_type(b.room_type_id)
        if b.status in ("expired", "cancelled") and rt is not None \
                and self.repo.free_rooms(rt, b.check_in, b.check_out, now) < b.rooms:
            b.notes = (b.notes + " PAID AFTER EXPIRY, ROOMS GONE: REFUND NEEDED").strip()
            b.razorpay_payment_id = payment_id or b.razorpay_payment_id
            self.repo.save_booking(b)
            self.alert_staff(f"REFUND NEEDED: {b.id} was paid after the hold expired and the rooms are gone "
                             f"({b.guest_name} {b.guest_phone}, Rs {b.advance}).", b.property_id)
            return "refund_needed"
        b.razorpay_payment_id = payment_id or b.razorpay_payment_id
        apply_payment(b, method="online", amount=b.advance if b.advance >= 1 else b.total,
                      ref=payment_id or link_id or b.payment_link_id or f"online-{b.id}", now=now)
        self.repo.save_booking(b)
        self.announce_paid(b)
        return "paid"

    def announce_paid(self, b: Booking) -> None:
        """Tell the guest their booking is confirmed and alert the staff (online or desk payment)."""
        prop = self.repo.get_property(b.property_id)
        rt = self.repo.get_room_type(b.room_type_id)
        text = t("paid", b.language, id=b.id, property=prop.name if prop else "", check_in=fmt_date(b.check_in),
                 check_out=fmt_date(b.check_out), rooms=b.rooms, checkin_time=prop.checkin_time if prop else "")
        if prop and prop.map_url:
            text += "\n" + t("map_line", b.language, url=prop.map_url)
        delivered = self._notify_guest(b, text, "paid", self._params(b))
        try:  # ask for the ID proof once (only inside the 24 h window; the day-before reminder covers the rest)
            if delivered:
                self.start_id_request(b)
        except Exception:
            log.exception("ID request for %s failed", b.id)
        self.alert_staff(f"New booking {b.id} PAID: {prop.name if prop else ''}, {rt.name if rt else ''} x {b.rooms}, "
                         f"{fmt_date(b.check_in)} to {fmt_date(b.check_out)}, {b.adults}A {b.children}C. "
                         f"{b.guest_name} {b.guest_phone}. Paid Rs {b.paid_amount} of Rs {b.total} "
                         f"({method_label(b.payment_method)}).", b.property_id)

    def payment_failed(self, *, booking_id: str = "", link_id: str = "") -> str:
        b = (self.repo.get_booking(booking_id) if booking_id else None) or (
            self.repo.find_booking_by_link(link_id) if link_id else None)
        if b is None or b.status == "paid":
            return "ignored"
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
        b.status = "expired"
        self.repo.save_booking(b)
        self._notify_guest(b, t("expired", b.language, id=b.id), "failed", self._params(b))
        return "expired"

    def _bot_asked_something(self, phone: str, st: dict[str, Any]) -> bool:
        """True when our last message to this guest waits for an answer: it has a question in it, a booking summary
        is waiting for their confirmation, or we have not written to them yet."""
        if (st.get("bk") or {}).get("chosen"):
            return True
        last = next((m for m in reversed(self.repo.list_chat(phone, limit=6)) if m.direction == "out"), None)
        return last is None or "?" in last.text

    def check_payments(self) -> int:
        """Ask the payment provider about every unpaid hold. A paid link is confirmed exactly as the webhook would."""
        link_status = getattr(self.payments, "link_status", None)
        if link_status is None:
            return 0
        found = 0
        for b in self.repo.list_bookings(status="hold", limit=200):
            if not b.payment_link_id:
                continue
            try:
                status = link_status(b.payment_link_id)
            except Exception as exc:  # the provider being slow must never stop the sweep
                log.warning("Payment check for %s failed: %s", b.id, exc)
                continue
            if status.get("paid") and self.mark_paid(booking_id=b.id, payment_id=status.get("payment_id", "")) == "paid":
                found += 1
        return found

    def sweep(self, now: datetime | None = None) -> dict[str, int]:
        """Run every minute or so: confirm payments Razorpay has but we missed, expire unpaid holds (and tell the
        guests), nudge holds about to expire."""
        now = now or self.clock.now()
        found = self.check_payments()
        expired = self.repo.expire_holds(now)
        for b in expired:
            self._notify_guest(b, t("expired", b.language, id=b.id), "failed", self._params(b))
        nudged = 0
        for b in self.repo.list_bookings(status="hold", limit=200):
            if (b.hold_expires_at and b.payment_link_url and "[reminded]" not in b.notes
                    and timedelta(0) < b.hold_expires_at - now <= timedelta(minutes=5)):
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
