"""WhatsApp ID-proof step after payment (the guest just sends a photo), the day-before reminder, and the
post-checkout feedback (one reply: a rating and an optional comment). All plain chat, no lists or buttons.

Mixed into BookingBot (flow.py). State lives in the guest's chat state next to the booking flow, so it never
disturbs a new booking the guest starts meanwhile:

    st["idp"] = {"booking_id", "guest_no"}
    st["idp_queue"] = [booking ids waiting their turn]      (a guest with two paid bookings)
    st["fb"]  = {"booking_id"}

Privacy: the photo is stored as the guest sent it; only the LAST 4 digits of a typed number are kept, and the
bot never repeats a number back. delete_after = check-out + KEEP_DAYS (the sweeper purges expired photos).
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from app.core.clock import to_ist
from app.core.phone import mask_phone
from app.resort import feedback
from app.resort.i18n import t
from app.resort.models import Booking, ChatMessage as Chat, IdProof
from app.whatsapp.client import WhatsAppError

log = logging.getLogger(__name__)

KEEP_DAYS = 30
MAX_BYTES = 5 * 1024 * 1024
ALLOWED_MIME = frozenset({"image/jpeg", "image/png", "image/webp"})
MARK_ASKED = "[id-asked]"
MARK_REMINDED = "[id-reminded]"
SKIP_WORDS = {"skip", "later", "no", "none", "nope", "illa"}
SKIP_PHRASE = re.compile(r"\b(later|skip)\b|(show|bring|give|submit)\w*.{0,25}check.?in|at check.?in$")
REMIND_FROM_HOUR, REMIND_TO_HOUR = 9, 20  # IST window for the reminder
LIVE_STATUSES = ("paid", "checked_in")


class IdProofMixin:
    # ------------------------------------------------------------------ helpers

    def _mark(self, booking_id: str, marker: str) -> None:
        fresh = self.repo.get_booking(booking_id)
        if fresh is not None and marker not in fresh.notes:
            fresh.notes = (fresh.notes + " " + marker).strip()
            self.repo.save_booking(fresh)

    def _id_lang(self, st: dict[str, Any], b: Booking) -> str:
        return st.get("lang") or b.language or "en"

    def _next_guest_no(self, b: Booking) -> int:
        proofs = self.repo.list_id_proofs(b.id)
        return max((p.guest_no for p in proofs), default=0) + 1

    def _save_proof(self, b: Booking, guest_no: int, id_type: str, status: str, **extra: Any) -> IdProof:
        existing = next((p for p in self.repo.list_id_proofs(b.id) if p.guest_no == guest_no), None)
        proof = IdProof(b.id, guest_no, id_type, self.clock.now(), status=status,
                        delete_after=b.check_out + timedelta(days=KEEP_DAYS), **extra)
        if existing is not None:
            proof.id = existing.id  # replace the earlier placeholder or photo for this guest
        return self.repo.save_id_proof(proof)

    # ------------------------------------------------------------------ asking

    def start_id_request(self, b: Booking) -> bool:
        """Ask for the ID proof of a newly paid booking, once. True when it was asked or queued."""
        if MARK_ASKED in b.notes or b.adults < 1:
            return False
        phone = b.guest_phone
        st = self.repo.get_state(phone)
        active = st.get("idp")
        if active and active.get("booking_id") != b.id:  # another booking is mid-way: this one waits its turn
            queue = st.setdefault("idp_queue", [])
            if b.id not in queue:
                queue.append(b.id)
            self.repo.set_state(phone, st)
            self._mark(b.id, MARK_ASKED)
            b.notes = (b.notes + " " + MARK_ASKED).strip()
            return True
        if self._next_guest_no(b) > b.adults:
            return False
        if not self._begin_id(phone, st, b):
            return False  # outside the 24 h window: the day-before reminder (a template) tries again
        self._mark(b.id, MARK_ASKED)
        b.notes = (b.notes + " " + MARK_ASKED).strip()
        return True

    def _begin_id(self, phone: str, st: dict[str, Any], b: Booking, *, ask: bool = True) -> bool:
        """Open the ID request. `ask=False` when a reminder message has already asked."""
        st["idp"] = {"booking_id": b.id, "guest_no": self._next_guest_no(b)}
        if ask and not self._send_id_ask(phone, st, b):
            st.pop("idp", None)
            return False
        self._save(phone, st)
        return True

    def _send_id_ask(self, phone: str, st: dict[str, Any], b: Booking) -> bool:
        lang, n, m = self._id_lang(st, b), st["idp"]["guest_no"], b.adults
        key = "id_body_first" if n == 1 else "id_body_next"
        return self._text(phone, t(key, lang, n=n, m=m, days=KEEP_DAYS))

    def _id_finish(self, phone: str, st: dict[str, Any], lang: str, *, later: bool) -> str:
        st.pop("idp", None)
        queue = st.get("idp_queue") or []
        self._text(phone, t("id_later_ok" if later else "id_done", lang))
        while queue:  # the next waiting booking, if it is still live
            nxt = self.repo.get_booking(queue.pop(0))
            if nxt is not None and nxt.status in LIVE_STATUSES and self._next_guest_no(nxt) <= nxt.adults:
                st["idp_queue"] = queue
                self._begin_id(phone, st, nxt)
                return "id_next_booking"
        st.pop("idp_queue", None)
        self._save(phone, st)
        return "id_done"

    def _id_advance(self, phone: str, st: dict[str, Any], b: Booking, lang: str) -> str:
        idp = st["idp"]
        if idp["guest_no"] < b.adults:
            idp["guest_no"] += 1
            self._save(phone, st)
            self._send_id_ask(phone, st, b)
            return "id_next_guest"
        return self._id_finish(phone, st, lang, later=False)

    # ------------------------------------------------------------------ answers

    def _id_step(self, phone: str, st: dict[str, Any], text: str) -> str | None:
        """Typed text while an ID is awaited: 'I will show it at check-in' closes it; anything else is a normal
        question for the AI (returns None)."""
        idp = st["idp"]
        b = self.repo.get_booking(idp["booking_id"])
        if b is None or b.status not in LIVE_STATUSES:
            st.pop("idp", None)
            self._save(phone, st)
            return None
        word = text.strip().lower()
        if word in SKIP_WORDS or (len(word.split()) <= 12 and SKIP_PHRASE.search(word)):
            for k in range(idp["guest_no"], b.adults + 1):
                self._save_proof(b, k, "later", "pending")
            return self._id_finish(phone, st, self._id_lang(st, b), later=True)
        return None

    def _id_media(self, phone: str, st: dict[str, Any], msg) -> str:
        """A photo/file from a guest who has an open ID request."""
        now = self.clock.now()
        self.repo.add_chat(Chat(phone, "in", f"[{msg.type}]", now, "text"))
        idp = st["idp"]
        b = self.repo.get_booking(idp["booking_id"])
        if b is None or b.status not in LIVE_STATUSES:
            st.pop("idp", None)
            self._save(phone, st)
            self._text(phone, t("unsupported", st.get("lang") or "en"))
            return "unsupported"
        lang = self._id_lang(st, b)
        try:
            self.wa.mark_read(msg.id)
        except Exception:
            pass
        if not msg.media_id:
            self._text(phone, t("id_photo_fail", lang))
            return "id_failed"
        try:
            data, mime = self.wa.download_media(msg.media_id, max_bytes=MAX_BYTES)
        except WhatsAppError as exc:
            log.warning("ID photo download failed for %s: %s", mask_phone(phone), exc)
            self._text(phone, t("id_photo_big" if "too large" in str(exc) else "id_photo_fail", lang))
            return "id_failed"
        mime = (mime or "").split(";")[0].strip().lower()
        if mime not in ALLOWED_MIME:
            self._text(phone, t("id_photo_bad", lang))
            return "id_rejected"
        if len(data) > MAX_BYTES:
            self._text(phone, t("id_photo_big", lang))
            return "id_failed"
        self._save_proof(b, idp["guest_no"], "other", "received", mime=mime, data=data)
        return self._id_advance(phone, st, b, lang)

    # ------------------------------------------------------------------ day-before reminder

    def remind_ids(self, now) -> int:
        """ONE gentle reminder the day before check-in (09:00-20:00 IST) for paid bookings without ID proofs."""
        local = to_ist(now)
        if not REMIND_FROM_HOUR <= local.hour < REMIND_TO_HOUR:
            return 0
        tomorrow = local.date() + timedelta(days=1)
        sent = 0
        for b in self.repo.list_bookings(status="paid", start=tomorrow, end=tomorrow + timedelta(days=1), limit=500):
            if b.check_in != tomorrow or MARK_REMINDED in b.notes:
                continue
            if len({p.guest_no for p in self.repo.list_id_proofs(b.id)}) >= b.adults:
                continue  # photos received, or the guest promised to show them at check-in
            prop = self.repo.get_property(b.property_id)
            delivered = self._notify_guest(b, t("id_reminder", b.language, id=b.id,
                                                property=prop.name if prop else ""), "id_reminder", self._params(b))
            self._mark(b.id, MARK_REMINDED)
            sent += 1
            if delivered:  # inside the 24 h window: their next photo counts as the ID
                st = self.repo.get_state(b.guest_phone)
                active = st.get("idp")
                if not active or active.get("booking_id") == b.id:
                    self._begin_id(b.guest_phone, st, b, ask=False)
        return sent

    # ------------------------------------------------------------------ feedback after checkout

    def announce_checked_out(self, b: Booking) -> bool:
        """Thank the guest and ask for a 1-5 rating (call this when staff check the guest out)."""
        prop = self.repo.get_property(b.property_id)
        lang = b.language
        if not self._notify_guest(b, t("fb_thanks", lang, property=prop.name if prop else ""), "feedback",
                                  self._params(b)):
            return False
        if not self._text(b.guest_phone, t("fb_body", lang)):
            return False
        st = self.repo.get_state(b.guest_phone)
        st["fb"] = {"booking_id": b.id}
        self._save(b.guest_phone, st)
        return True

    @staticmethod
    def _parse_feedback(text: str) -> tuple[int, str] | None:
        """(rating, comment) from '4', '5 loved it', '★★★★', '4/5 great food' or 'great food 4'. Else None.
        A digit in the middle of a sentence ('we booked 2 rooms') is never taken as the rating."""
        s = text.strip()
        if m := re.match(r"^\W*([1-5])(?!\d)(?:\s*/\s*5)?\W*(.*)$", s, re.S):
            return int(m[1]), m[2].strip()
        if m := re.match(r"^\s*(★{1,5})\W*(.*)$", s, re.S):
            return len(m[1]), m[2].strip()
        if m := re.search(r"(?<!\d)([1-5])\s*/\s*5(?!\d)", s):
            return int(m[1]), (s[:m.start()] + s[m.end():]).strip(" ,.-")
        if m := re.match(r"^(.*?)\W*(?<!\d)([1-5])\W*$", s, re.S):
            return int(m[2]), m[1].strip()
        return None

    def _fb_step(self, phone: str, st: dict[str, Any], text: str) -> str | None:
        fb = st["fb"]
        b = self.repo.get_booking(fb["booking_id"])
        if b is None:
            st.pop("fb", None)
            self._save(phone, st)
            return None
        if text.strip().lower() in SKIP_WORDS:
            st.pop("fb", None)
            self._save(phone, st)
            return "feedback_skipped"
        parsed = self._parse_feedback(text)
        if parsed is None:
            return None  # not a rating: a normal message for the AI (the request stays open)
        rating, comment = parsed
        feedback.save_feedback(self.repo, b.id, rating, comment if len(comment) >= 2 else "", self.clock.now())
        st.pop("fb", None)
        self._save(phone, st)
        self._text(phone, t("fb_done", st.get("lang") or b.language))
        return "feedback_done"
