"""The one enquiry object (docs/AI_AGENT_DESIGN.md section 2).

The AI sees it every turn as the CURRENT ENQUIRY block (`render()`); only tools change it. It is kept in the guest's
chat state under its own key (`agent`), so the old bot's state (`bk`, `checked`, ...) is never touched.
It holds no price: amounts only ever come from tool results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

STATE_KEY = "agent"
LANGUAGES = ("en", "manglish", "tanglish", "hinglish")

# What the guest or the AI should do next. `check` is the one value beyond the design's list: everything is known but
# no room check has run yet (a tool normally runs it in the same call).
NEXT_STEPS = ("ask_location", "ask_details", "check", "ask_extra_bed", "ask_contact", "pay", "done")

_NEXT_TEXT = {
    "ask_location": "the resort is not known: ask the location",
    "ask_details": "dates or persons are missing: send the booking form",
    "check": "everything is known: check the rooms",
    "ask_extra_bed": "a room will hold 3 adults and 'Do you need an extra bed?' was asked: wait for the answer, then "
                     "call update_enquiry with extra_beds (a number for yes, 0 for no)",
    "ask_contact": "rooms are available: ask for the guest's name and mobile number",
    "pay": "guest should pay the advance",
    "done": "booking paid and confirmed",
}


def _day(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


@dataclass
class Enquiry:
    place: str | None = None  # resort slug / property id
    check_in: date | None = None
    check_out: date | None = None
    adults: int | None = None
    children: int | None = None
    rooms: int | None = None  # None = the code picks the fewest
    room_picks: list[dict[str, Any]] = field(default_factory=list)  # [{"room_type": "cottage", "count": 1}, {"room_number": "103"}]
    extra_beds: int = 0
    early_checkin: bool = False  # only when the guest asked: it changes the room check's price
    checked: dict[str, Any] | None = None  # last availability result written by code: {"available", "summary", "rooms"}
    booking_id: str | None = None  # internal only, never shown to the guest
    link_open: bool = False
    paid: bool = False
    language: str = "en"  # en | manglish | tanglish | hinglish
    photo_wish: dict[str, Any] | None = None  # photos asked for before any resort: {"kind", "room_type"}; sent once the place is known
    intro_sent: str | None = None  # the resort whose glimpse (Property.intro) was already sent in this enquiry
    contact: dict[str, Any] | None = None  # {"name", "mobile"} of a payment link closed by a layout change, kept for the new link
    bed_asked_for: str | None = None  # "adults|rooms": the layout the extra-bed question was asked for (asked once)
    bed_answered_for: str | None = None  # the layout the guest answered it for (or asked for a bed on their own)

    # ------------------------------------------------------------------ computed

    @property
    def nights(self) -> int | None:
        return (self.check_out - self.check_in).days if self.check_in and self.check_out else None

    @property
    def missing(self) -> list[str]:
        """What the next step still needs from the guest."""
        need = []
        if not self.place:
            need.append("place")
        if not self.check_in:
            need.append("check_in")
        if not self.check_out:
            need.append("check_out")
        if not self.adults:
            need.append("adults")
        return need

    @property
    def next_step(self) -> str:
        if self.paid:
            return "done"
        if self.booking_id and self.link_open:
            return "pay"
        if not self.place:
            return "ask_location"
        if self.missing:
            return "ask_details"
        if self.checked is None:
            return "check"
        if self.checked.get("available") is False:
            return "ask_details"  # not available: other dates are needed
        if self.checked.get("extra_bed_pending"):
            return "ask_extra_bed"
        return "ask_contact"

    # ------------------------------------------------------------------ the AI's view

    def render(self, place_label: str = "") -> str:
        """The CURRENT ENQUIRY block: plain lines, no amounts, no booking id."""
        place = place_label or self.place
        parts = [f"place: {place}" if place else "place: not known yet"]
        if self.check_in:
            parts.append(f"check-in {_day(self.check_in)}")
        if self.check_out:
            n = self.nights
            parts.append(f"check-out {_day(self.check_out)}" + (f" ({n} night{'s' if n != 1 else ''})" if n else ""))
        if self.adults:
            kids = f" + {self.children} child{'ren' if self.children != 1 else ''}" if self.children else ""
            parts.append(f"{self.adults} adult{'s' if self.adults != 1 else ''}{kids}")
        if self.rooms:
            parts.append(f"rooms: {self.rooms}")
        elif self.checked and self.checked.get("rooms"):
            parts.append(f"rooms: {self.checked['rooms']} (code picked)")
        if self.room_picks:
            parts.append("picked: " + ", ".join(self._pick(p) for p in self.room_picks))
        if self.extra_beds:
            parts.append(f"extra beds: {self.extra_beds}")
        if self.early_checkin:
            parts.append("early check-in asked")
        lines = ["CURRENT ENQUIRY", " · ".join(parts)]
        if self.checked is not None:
            if self.checked.get("available") is False:
                lines.append("checked: not available for these dates")
            else:
                lines.append("checked: available" + (f" — {self.checked['summary']}" if self.checked.get("summary") else ""))
        if self.paid:
            lines.append("payment: paid, booking confirmed")
        elif self.booking_id:
            lines.append("payment link: " + ("open, not paid" if self.link_open else "none open (expired or not made)"))
        if self.missing:
            lines.append("still needed: " + ", ".join(m.replace("_", "-") for m in self.missing))
        lines.append(f"next step: {_NEXT_TEXT[self.next_step]}")
        return "\n".join(lines)

    @staticmethod
    def _pick(p: dict[str, Any]) -> str:
        name = p.get("room_type") or (f"room {p['room_number']}" if p.get("room_number") else "?")
        return f"{p['count']} x {name}" if p.get("count") else str(name)

    # ------------------------------------------------------------------ storing

    def to_dict(self) -> dict[str, Any]:
        return {"place": self.place, "check_in": self.check_in.isoformat() if self.check_in else None,
                "check_out": self.check_out.isoformat() if self.check_out else None, "adults": self.adults,
                "children": self.children, "rooms": self.rooms, "room_picks": self.room_picks,
                "extra_beds": self.extra_beds, "early_checkin": self.early_checkin, "checked": self.checked, "booking_id": self.booking_id,
                "link_open": self.link_open, "paid": self.paid, "language": self.language,
                "photo_wish": self.photo_wish, "intro_sent": self.intro_sent,
                "bed_asked_for": self.bed_asked_for, "bed_answered_for": self.bed_answered_for,
                "contact": self.contact}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Enquiry:
        d = data or {}

        def day(key: str) -> date | None:
            try:
                return date.fromisoformat(d[key]) if d.get(key) else None
            except ValueError:
                return None

        lang = d.get("language") if d.get("language") in LANGUAGES else "en"
        return cls(place=d.get("place"), check_in=day("check_in"), check_out=day("check_out"), adults=d.get("adults"),
                   children=d.get("children"), rooms=d.get("rooms"), room_picks=list(d.get("room_picks") or []),
                   extra_beds=int(d.get("extra_beds") or 0), early_checkin=bool(d.get("early_checkin")),
                   checked=d.get("checked"), booking_id=d.get("booking_id"),
                   link_open=bool(d.get("link_open")), paid=bool(d.get("paid")), language=lang,
                   photo_wish=d.get("photo_wish") or None, intro_sent=d.get("intro_sent") or None,
                   bed_asked_for=d.get("bed_asked_for") or None, bed_answered_for=d.get("bed_answered_for") or None,
                   contact=d.get("contact") or None)

    @classmethod
    def load(cls, repo: Any, phone: str) -> Enquiry:
        return cls.from_dict(repo.get_state(phone).get(STATE_KEY))

    def save(self, repo: Any, phone: str) -> None:
        """Writes only the `agent` key; every other key of the guest's state (the old bot's) stays as it is."""
        state = repo.get_state(phone)
        state[STATE_KEY] = self.to_dict()
        repo.set_state(phone, state)
