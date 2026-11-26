"""The agent's tools (docs/AI_AGENT_DESIGN.md section 3). AI-first simplification (2026-11-26): tools return FACTS only
and the AI writes every guest message itself with reply() (turn.py). Code still sends exactly: the booking summary, the
payment link message, the paid confirmation, photos, room labels, the resort's glimpse and the map link.

The AI passes structured arguments; code does everything else. Nothing here reads or matches the guest's own words:
no regex, no keyword list. A tool compares the stay the AI passed with the saved Enquiry and runs the room check with the
existing availability and pricing code (availability.py, the BookingBot helpers, the repository).

Every result has the same shape: {ok, facts, sent, next_step, note}.
  facts      what the AI tells the guest in its own words (rooms, availability, price, what is missing)
  sent       the outbox keys this call queued (summary, pay, photos ...): the AI must not write those again
  next_step  the Enquiry's next step after the call
"""

from __future__ import annotations

import difflib
import logging
import time
from datetime import date, timedelta
from typing import Any

from app.providers.llm import ToolSpec
from app.resort import availability as av
from app.resort.agent import conversation
from app.resort.agent import photos_plan as pp
from app.resort.agent.language import TurnLanguage
from app.resort.agent.outbox import Outbox
from app.resort.agent.state import LANGUAGES, Enquiry
from app.resort.chat import _Turn, _count_name, _money, close_word
from app.resort.i18n import status_name, t
from app.resort.models import Property, RoomType
from app.resort.refunds import cancellation_terms

log = logging.getLogger(__name__)

I18N_LANG = {"en": "en", "manglish": "mn", "tanglish": "tn", "hinglish": "hn"}

ALREADY_SENT = "Already sent to the guest (see sent): never write it again."  # the note's start when a tool sent messages
SENT_NOTE = "Tell the guest what matters with reply(), using the owner's wording from the prompt."
UNCHANGED_NOTE = "The stay is unchanged. Answer what the guest asked with reply()."

# talk_to_owner(reason): the line for the team. The tool sends the guest nothing: the AI writes the reply itself.
OWNER_REASONS: dict[str, str] = {
    "pay_terms": "guest wants other payment terms (smaller advance / cash / pay at the property / pay later)",
    "cannot_answer": "the bot could not answer the guest",
    "call_request": "the guest wants to talk to the team",
    "change_or_cancel_paid": "the guest wants to change or cancel a PAID booking",
    "other": "the guest needs the team",
}

LANGUAGE_FIELD = {"type": "string", "enum": list(LANGUAGES),
                  "description": "language = the language of the guest's LATEST message only, never an earlier one "
                                 "(plain English → en): en = English; manglish = Malayalam "
                                 "words in English letters (venam, undo, aano, alle); tanglish = Tamil words in English "
                                 "letters (venum, irukka, irukkom, vanakkam, la); hinglish = Hindi words in English "
                                 "letters (chahiye, hai, kya)"}

DATE_FIELD = {"type": "object",
              "description": "Copy only what the guest said. Never fill month or year yourself. '20 Nov' → {day: 20, "
                             "month: 11}. A month word the guest wrote is ALWAYS copied. Only leave month null if the "
                             "guest wrote no month.",
              "properties": {"day": {"type": "integer", "description": "1-31"},
                             "month": {"type": "integer", "description": "1-12, or null"},
                             "year": {"type": "integer", "description": "null if not said"}},
              "required": ["day"]}

# the tools that send something to the guest or the team: each runs at most ONCE per turn (fix round 2)
ONCE_TOOLS = ("send_payment_link", "send_photos", "create_booking", "check_payment", "talk_to_owner", "send_map")
ONCE_NOTE = "Already done this turn: nothing more was sent. Do not call it again."

# Descriptions are short (one example each) and do not repeat the system prompt: they are sent on every call (cached).
TOOL_SCHEMAS = [
    ToolSpec(
        "update_enquiry",
        "Any wish to book, see rooms or know availability (even 'I need a room'), and anything the guest says about "
        "their stay (place, dates, nights, adults, rooms, a room they CHOOSE, 'make it 5 persons'). "
        "A place or resort name on its own ('Alleppey', 'kanthaloor', even misspelt) IS booking information → call "
        "update_enquiry(place=...). Pass the FULL stay (CURRENT ENQUIRY + what the guest just said); leave out what is "
        "unknown. Give check_out OR nights: code works out the other. It returns what is missing and checks the "
        "rooms (room photos and labels go out by themselves); then tell the guest with reply().",
        {"type": "object", "properties": {
            "place": {"type": "string", "description": "the resort or town as the guest wrote it in this chat; never one the "
                                                     "guest did not name"},
            "check_in": DATE_FIELD,
            "check_out": DATE_FIELD,
            "nights": {"type": "integer", "description": "instead of check_out"},
            "adults": {"type": "integer", "description": "adults above 6 years, as the guest said (never assume)"},
            "rooms": {"type": "integer", "description": "only if the guest said a number of rooms"},
            "room_picks": {"type": "array", "description": "ONLY a clear CHOICE ('take X', 'change to X', 'X venam', "
                                                         "'book X', 'room 103', a swipe-reply 'this one' on a room's "
                                                         "photo), one entry per kind; [] clears it. A QUESTION about "
                                                         "another room ('do you have X also?', 'is there a cottage?', "
                                                         "'any deluxe?') is NOT a choice → send_photos, never "
                                                         "room_picks.",
                           "items": {"type": "object", "properties": {
                               "room_type": {"type": "string", "description": "a room type name from FACTS"},
                               "room_number": {"type": "string"},
                               "count": {"type": "integer"}}}},
            "exclude_room_types": {"type": "array", "items": {"type": "string"},
                                   "description": "use ONLY when the guest says they don't want a type ('no cottage')"},
            "extra_beds": {"type": "integer", "description": "only the guest's answer to the extra-bed question (0 = "
                                                             "no) or an extra bed they ask for themselves"},
            "early_checkin": {"type": "boolean", "description": "true only if the guest asked for early check-in"},
            "reason": {"type": "string", "enum": list(pp.FORM_REASONS),
                       "description": "what the guest asked: price (a rate or cost), availability (are rooms free), "
                                      "booking (anything else; the default). Comes back as form_reason."},
            "language": LANGUAGE_FIELD}}),
    ToolSpec(
        "get_price",
        "Every question about price, rate, cost, total or advance (e.g. 'cost of the cottage'). Answers for the stay "
        "already given (or on the open link): the amounts come back in facts. No stay known yet: it returns what is "
        "missing (form_reason price).",
        {"type": "object", "properties": {
            "room_type": {"type": "string", "description": "only if the guest named a room type"},
            "language": LANGUAGE_FIELD}}),
    ToolSpec(
        "send_photos",
        "The guest wants to see rooms or the resort, in any words ('pics?'). kind: room_type (also pass room_type), "
        "resort, other_room, all_rooms, default (no room named), more. purpose: see = they only want to look (no "
        "availability check, never say free); consider = another room, switching, or is it free (checks THEIR dates). "
        "No dates yet: no photos unless insist=true (they insist: 'just send photos', 'photos first', asked again). "
        "It never switches their room: only a clear CHOICE ('take X', 'X venam') → update_enquiry room_picks.",
        {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(pp.KINDS)},
            "purpose": {"type": "string", "enum": ["see", "consider"]},
            "insist": {"type": "boolean", "description": "true only when no dates are known and the guest insists on "
                                                         "photos first"},
            "room_type": {"type": "string", "description": "only for kind room_type: a room type name from FACTS"},
            "language": LANGUAGE_FIELD},
         "required": ["kind", "purpose"]}),
    ToolSpec(
        "send_map",
        "The guest asks for the location, map, pin or directions.",
        {"type": "object", "properties": {"language": LANGUAGE_FIELD}}),
    ToolSpec(
        "create_booking",
        "Rooms were found available and the guest gives a mobile number (and a name if they gave one, else empty). "
        "'same number' → mobile 'same'. Never ask 'shall I confirm?'. The owner's summary and payment link go out by "
        "themselves.",
        {"type": "object", "properties": {
            "guest_name": {"type": "string", "description": "the name the guest gave, or empty"},
            "mobile": {"type": "string", "description": "the number the guest gave, or 'same'"},
            "language": LANGUAGE_FIELD},
         "required": ["mobile"]}),
    ToolSpec(
        "check_payment",
        "The guest says they paid, sends a payment screenshot, or asks if the payment arrived. Only this tool can "
        "confirm a payment.",
        {"type": "object", "properties": {"language": LANGUAGE_FIELD}}),
    ToolSpec(
        "send_payment_link",
        "Link not working, lost, or 'send it again' → reason lost. If the guest wants to hold the room, reserve, or pay "
        "later/tonight/tomorrow with an open enquiry → send_payment_link(reason='hold'). 'not paid yet' → not_paid_yet. "
        "An expired link is checked again by code.",
        {"type": "object", "properties": {
            "reason": {"type": "string", "enum": ["lost", "hold", "not_paid_yet"]},
            "language": LANGUAGE_FIELD},
         "required": ["reason"]}),
    ToolSpec(
        "my_bookings",
        "The guest asks about a booking they already have (dates, status, paid, balance) or wants to cancel (gives the "
        "charge if cancelled today). Read only.",
        {"type": "object", "properties": {}}),
    ToolSpec(
        "change_name",
        "The guest corrects their name ('my name is not X, it is Y'). Never start the booking again for it.",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "the correct name, in English letters"},
            "language": LANGUAGE_FIELD},
         "required": ["name"]}),
    ToolSpec(
        "talk_to_owner",
        "Only for what you truly cannot answer or handle: it alerts the team with staff_note and sends the guest "
        "NOTHING (you write the reply yourself). Reasons: pay_terms = other payment terms (smaller advance, cash, pay "
        "at the property); cannot_answer = not in FACTS; call_request = they want the team or a call; "
        "change_or_cancel_paid = change or cancel a PAID booking; other = complaint, refund, second discount ask, an "
        "instruction for the team ('don't call my father').",
        {"type": "object", "properties": {
            "reason": {"type": "string", "enum": list(OWNER_REASONS)},
            "staff_note": {"type": "string", "description": "for the team only, e.g. 'asks full refund'"},
            "language": LANGUAGE_FIELD},
         "required": ["reason"]}),
    ToolSpec(
        "set_language",
        "The guest asks you to use a language ('English please', 'speak in English', 'Malayalam il parayamo'). That "
        "language then holds for the rest of this conversation. Sends nothing.",
        {"type": "object", "properties": {"language": LANGUAGE_FIELD}}),
    ToolSpec(
        "reply",
        "The ONLY way to write to the guest. Everything else you write is a private note that is never sent. Call it "
        "once per turn with your whole answer (it may come together with other tool calls).",
        {"type": "object", "properties": {
            "text": {"type": "string", "description": "the message for the guest"},
            "language": LANGUAGE_FIELD},
         "required": ["text"]}),
    ToolSpec(
        "recall_old_chat",
        "Only when the guest refers to an EARLIER conversation ('as I told you'). Never for a fresh request. Read only.",
        {"type": "object", "properties": {"query": {"type": "string", "description": "what you are looking for"}}}),
]
# fix round 5: `language` is REQUIRED in every tool that sends the guest text (code still falls back to the last one)
for _spec in TOOL_SCHEMAS:
    if "language" in _spec.parameters["properties"] and "language" not in _spec.parameters.get("required", []):
        _spec.parameters["required"] = [*_spec.parameters.get("required", []), "language"]
TOOLS = TOOL_SCHEMAS  # the same list, under the name AI-2a used


def pay_text(b: Any, lang: str, now: Any) -> str:
    """The owner's payment-link text for a held booking."""
    minutes = max(1, int((b.hold_expires_at - now).total_seconds() // 60))
    return t("pay", lang, id=b.id, minutes=minutes, advance=f"{b.advance:,}", url=b.payment_link_url)


def live_link(bot: Any, phone: str) -> Any:
    """The guest's held booking whose payment link is still open, or None."""
    now = bot.clock.now()
    return next((b for b in bot.repo.list_bookings(phone=phone, status="hold", limit=5)
                 if b.payment_link_url and b.hold_expires_at and b.hold_expires_at > now), None)


def _int(args: dict[str, Any], key: str, default: int | None = None) -> int | None:
    """An integer argument; missing or null = `default` (the saved value)."""
    value = args.get(key)
    if value is None or value == "":
        return default
    return int(value)


def _beds_on(booking: Any) -> int:
    """The extra beds on a booking: its notes carry 'Extra beds: N'."""
    for note in (booking.notes or "").split(";"):
        if note.strip().startswith("Extra beds:"):
            digits = note.split(":", 1)[1].strip()
            return int(digits) if digits.isdigit() else 0
    return 0


def _mobile_on(booking: Any) -> str:
    """The mobile number on a booking: its code-written notes carry 'Mobile: N' ('' when none)."""
    for note in (booking.notes or "").split(";"):
        if note.strip().startswith("Mobile:"):
            return note.split(":", 1)[1].strip()
    return ""


def _style(args: dict[str, Any], saved: str) -> str:
    """The language style the AI passed, any case; missing or not one of LANGUAGES -> the previous turn's (saved)."""
    said = str(args.get("language") or "").strip().lower()
    return said if said in LANGUAGES else saved


# ------------------------------------------------------------------ dates: the AI copies what the guest said, code builds


def _date_parts(value: Any) -> tuple[int, int | None, int | None] | None:
    """(day, month or None, year or None) from the AI's {day, month, year} (a full 'YYYY-MM-DD' string is also taken).
    None when nothing was passed. ValueError when it is not a date."""
    if value is None or value == "" or value == {}:
        return None
    if isinstance(value, str):
        d = date.fromisoformat(value.strip())
        return d.day, d.month, d.year
    if not isinstance(value, dict):
        raise ValueError("not a date object")

    def num(key: str) -> int | None:
        raw = value.get(key)
        return None if raw is None or raw == "" else int(raw)

    day, month, year = num("day"), num("month"), num("year")
    if day is None or not 1 <= day <= 31 or (month is not None and not 1 <= month <= 12):
        raise ValueError("day 1-31, month 1-12")
    if year is not None and year < 100:
        year += 2000  # '26' -> 2026
    return day, month, year


def _month_after(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _day_from(day: int, year: int, month: int, earliest: date, strictly_after: bool = False) -> date | None:
    """The first date with this day number in (year, month) or a later month, on or after `earliest` (after it when
    `strictly_after`). A month that lacks the day ('31' in November) is skipped."""
    for _ in range(14):
        try:
            d = date(year, month, day)
        except ValueError:
            d = None
        if d is not None and (d > earliest if strictly_after else d >= earliest):
            return d
        year, month = _month_after(year, month)
    return None


def _date_from(day: int, month: int, earliest: date, strictly_after: bool = False) -> date | None:
    """The next time this day and month come, on or after `earliest` (after it when `strictly_after`)."""
    for year in range(earliest.year, earliest.year + 9):  # 29 Feb waits for a leap year
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d > earliest if strictly_after else d >= earliest:
            return d
    return None


RECENT_PAST_DAYS = 30  # owner rule (fix round 4): a date without a year that passed up to 30 days ago is told past_date


def _last_time(day: int, month: int, today: date) -> date | None:
    """The latest date with this day and month on or before today (this year or an earlier one)."""
    for year in range(today.year, today.year - 9, -1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d <= today:
            return d
    return None


def build_check_in(parts: tuple[int, int | None, int | None], today: date) -> tuple[date | None, bool]:
    """(check-in, told): no month -> the next time that day comes; no year -> a date that passed only days ago (up to
    30) is that past date (told=True: the guest gets past_date), else the next time it comes (10 Jan on 25 Nov -> next
    January); a full date stays as given (told=True: past_date if it has passed)."""
    day, month, year = parts
    if month is None:
        return _day_from(day, today.year, today.month, today), False
    if year is None:
        last = _last_time(day, month, today)
        if last is not None and 0 < (today - last).days <= RECENT_PAST_DAYS:
            return last, True
        return _date_from(day, month, today), False
    return date(year, month, day), True


def build_check_out(parts: tuple[int, int | None, int | None], check_in: date) -> date | None:
    """No month -> the check-in's month, or the next month when that would not be after the check-in; no year -> the
    check-in's year, or the next year when that would not be after it."""
    day, month, year = parts
    if month is None:
        return _day_from(day, check_in.year, check_in.month, check_in, strictly_after=True)
    if year is None:
        return _date_from(day, month, check_in, strictly_after=True)
    return date(year, month, day)


# ------------------------------------------------------------------ places: a fact check against the dashboard names


def _words(text: str) -> list[str]:
    """Lower-case words of a text (letters and digits only). Plain string methods, no pattern matching."""
    return "".join(ch if ch.isalnum() else " " for ch in (text or "").casefold()).split()


def _squash(word: str) -> str:
    """'aalappee' -> 'alape': doubled letters once (speech-to-text spellings)."""
    out = ""
    for ch in word:
        if not out or out[-1] != ch:
            out += ch
    return out


def same_name(said: str, name: str) -> bool:
    """A word the guest wrote is this dashboard name, give or take a letter or two ('allepy' ~ 'alleppey'). Short words
    must match exactly."""
    if said == name:
        return True
    if min(len(said), len(name)) < 5 or said[0] != name[0]:
        return False
    return (difflib.SequenceMatcher(None, said, name).ratio() >= 0.75
            or difflib.SequenceMatcher(None, _squash(said), _squash(name)).ratio() >= 0.8)


def _label(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


def towns_and(bot: Any) -> str:
    """'Alleppey and Kanthalloor': where the OPEN resorts are, from the dashboard (never written in code)."""
    towns: list[str] = []
    for p in bot._places():
        town = (p.city or p.place or p.name).split(",")[0].strip()
        if p.active and town and town.casefold() not in (x.casefold() for x in towns):
            towns.append(town)
    if not towns:
        return ""
    return towns[0] if len(towns) == 1 else ", ".join(towns[:-1]) + " and " + towns[-1]


# send_photos found nothing to send: what the AI tells the guest instead
PHOTO_PROBLEMS = {
    "photos_sent": lambda room: {"all_photos_already_sent": True},
    "no_other_room": lambda room: {"no_other_room_free_for_your_dates": True},
    "room_not_free": lambda room: {"room_not_free_for_your_dates": room},
    "other_free": lambda room: {"also_free_for_your_dates": room},
}


class AgentTools:
    """The tools of one guest turn. `bot` is the BookingBot: its repository, clock, payments and existing helpers."""

    def __init__(self, bot: Any, phone: str, outbox: Outbox, guest_text: str | None = None,
                 lang: TurnLanguage | None = None) -> None:
        self.bot, self.phone, self.out = bot, phone, outbox
        # this turn's language (language.py; run_turn passes it): every call's `language` goes through it, so a missing
        # value never falls back to another conversation's language; None = outside a guest turn (the saved one)
        self.lang = lang
        # the guest's message of this turn (run_turn passes it); None = a call outside a guest turn: places not checked
        self.guest_text = guest_text
        self.ran: list[str] = []  # the tools that ran this turn (ONCE_TOOLS run at most once)

    @property
    def sending_ran(self) -> bool:
        """A tool that sends something (ONCE_TOOLS) ran this turn."""
        return any(name in ONCE_TOOLS for name in self.ran)

    # ------------------------------------------------------------------ helpers

    def _result(self, ok: bool, facts: dict[str, Any], start: int, enq: Enquiry, note: str) -> dict[str, Any]:
        sent = self.out.keys[start:]
        if any(not k.startswith("staff:") and k != "confirmed" for k in sent) and not note.startswith(ALREADY_SENT):
            note = f"{ALREADY_SENT} {note}"  # every tool that sent the guest something says so first
        return {"ok": ok, "facts": facts, "sent": sent, "next_step": enq.next_step, "note": note}

    def _only_resort(self) -> Property | None:
        """The one open resort, when there is exactly one: then the place is never asked."""
        places = [p for p in self.bot._places() if p.active]
        return places[0] if len(places) == 1 else None

    def _guest_words(self) -> list[str]:
        """Every word the guest wrote in this conversation (since a restart or a long gap), this turn's message too."""
        rows = self.bot.repo.list_chat(self.phone, limit=200)
        said = [m.text for m in rows[conversation.start_index(rows):] if m.direction == "in"]
        return [w for text in [*said, self.guest_text or ""] for w in _words(text)]

    def _place_said(self, place: str) -> bool:
        """The place the AI passed is one the guest wrote in this chat: its own words, or a dashboard name / alias of the
        resort it stands for, as written or misspelt. A fact check against the dashboard, never a reading of intent."""
        if self.guest_text is None:
            return True  # not a guest turn (code calling the tool itself)
        said = set(self._guest_words())
        own = [w for w in _words(place) if len(w) >= 3]
        if own and all(any(same_name(s, w) for s in said) for w in own):
            return True
        props = [p for p in self.bot.repo.list_properties(active_only=False) if self._names_place(p, place)]
        return any(same_name(s, n) for p in props for n in self.bot._place_words(p) for s in said)

    def _names_place(self, prop: Property, place: str) -> bool:
        """The AI's `place` is this resort: one of its dashboard names or aliases, as written or misspelt (a 5-letter start
        is enough: 'Kanthal')."""
        names = self.bot._place_words(prop)
        return any(same_name(w, n) or (len(w) >= 5 and n.startswith(w[:5])) for w in _words(place) for n in names)

    def _our_place(self, place: str) -> Property | None:
        """The open resort the AI's `place` names, or None when it names none of ours (or more than one)."""
        hits = [p for p in self.bot._places() if p.active and self._names_place(p, place)]
        return hits[0] if len(hits) == 1 else None

    def _towns_and(self) -> str:
        return towns_and(self.bot)

    def _numbers(self, prop: Property | None) -> str:
        """The team's numbers for this resort (else every resort's), as the guest is given them."""
        return self.bot._owner_numbers({"form_place": prop.id if prop else None})

    def _room(self, rooms: list[RoomType], name: str) -> RoomType | None:
        """A room type by the name the AI passed (any case, a part of the name, or a close spelling)."""
        return pp.find_room(self.bot, rooms, name)

    def _closed_place(self, place: str) -> Property | None:
        """A resort switched off in the dashboard that the AI's `place` names (and no open resort does)."""
        if self.bot._named_place(place) is not None:
            return None
        return self.bot._closed_place(place, True)

    def _picks(self, prop: Property, picks: list[dict[str, Any]], exclude: list[str]
               ) -> tuple[list[str], list[dict[str, Any]], str]:
        """The AI's room choices as room type ids (one per room), the picks to remember, and a problem text ('' = ok)."""
        rooms = self.bot.repo.list_room_types(prop.id)
        ids: list[str] = []
        for p in picks:
            number = str(p.get("room_number") or "").strip().lstrip("#")
            if number:
                rt = next((r for r in rooms if number in r.room_list), None)
                if rt is None:
                    return [], [], f"there is no room {number}; the room numbers are in the FACTS"
            elif str(p.get("room_type") or "").strip():
                rt = self._room(rooms, str(p["room_type"])) or self.bot._room_named(str(p["room_type"]), rooms)
                if rt is None:
                    return [], [], f"there is no room type '{p['room_type']}'; the room types are in the FACTS"
            else:
                continue
            ids += [rt.id] * max(1, int(p.get("count") or 1))
        if not ids and exclude:
            gone = {rt.id for name in exclude if (rt := self._room(rooms, str(name))) is not None}
            others = [rt for rt in rooms if rt.id not in gone]
            if not others:
                return [], [], "every room type was excluded; ask the guest which room they want"
            ids = [others[0].id]  # the owner's top-priority room type the guest did not rule out
        kept = [{"room_type": next(r.name for r in rooms if r.id == i), "count": ids.count(i)} for i in dict.fromkeys(ids)]
        return ids, kept, ""

    def _plan(self, prop: Property, bk: dict[str, Any], wanted: int) -> tuple[list[dict[str, Any]], list[tuple[RoomType, int]] | None]:
        """The rooms the stay needs: [{rt, rooms, beds}] from the existing room-priority code, or [] when none is free."""
        bot = self.bot
        mix = bot._mix(prop, bk, wanted)
        if mix:  # the top room type cannot take the whole group: its free rooms first, then the next room type
            return [{"rt": rt, "rooms": n, "beds": 0} for rt, n in mix], mix
        fits = bot._fits(prop, bk)
        if not fits:
            return [], None
        rt, ok = fits[0]
        rooms = wanted if wanted in ok else ok[0]
        return [{"rt": rt, "rooms": rooms, "beds": 0}], None  # an extra bed is never added by itself (_apply_beds)

    @staticmethod
    def _apply_beds(parts: list[dict[str, Any]], beds: int) -> None:
        """The extra beds the guest said yes to (or asked for), at most one per room, over the rooms in order."""
        left = max(0, beds)
        for part in parts:
            part["beds"] = min(left, part["rooms"])
            left -= part["beds"]

    def _price_of(self, prop: Property, parts: list[dict[str, Any]], ci: date, co: date,
                  early: bool = False) -> dict[str, Any]:
        """Totals from the dashboard rates now (an early check-in adds its fee per room). These are the only amounts the
        AI may use."""
        total = advance = fee_total = 0
        rates: set[int] = set()
        for part in parts:
            q = av.quote(prop, part["rt"], self.bot.repo.list_rate_rules(part["rt"].id), ci, co, part["rooms"], part["beds"])
            fee = prop.early_checkin_rate * part["rooms"] if early else 0
            part_total = q.total + fee
            part_advance = round(part_total * prop.advance_pct / 100) if fee else q.advance
            total += part_total
            advance += part_advance
            fee_total += fee
            rates |= {rate for _, rate in q.per_night}
            part["total"], part["advance"] = part_total, part_advance
        price: dict[str, Any] = {"total": total, "advance_now": advance, "balance_at_property": total - advance}
        if fee_total:
            price["early_checkin_fee"] = fee_total
        if len(rates) == 1 and len(parts) == 1:
            price["per_room_per_night"] = rates.pop()
        return price

    # ------------------------------------------------------------------ update_enquiry

    def update_enquiry(self, args: dict[str, Any]) -> dict[str, Any]:
        bot, phone = self.bot, self.phone
        repo, today = bot.repo, bot._today()
        start = len(self.out.keys)
        cur = Enquiry.load(repo, phone)
        if cur.paid:  # a paid stay is over: a new enquiry starts (the language is kept)
            cur = Enquiry(language=cur.language)
        style = _style(args, cur.language)
        lang = I18N_LANG[style]
        enq = Enquiry.from_dict(cur.to_dict())
        enq.language = style
        held = bot._open_hold(phone)
        enq.booking_id, enq.link_open = (held[0].id, True) if held else (None, False)

        def done(ok: bool, facts: dict[str, Any], note: str) -> dict[str, Any]:
            facts = {**not_ours, **facts}
            enq.started = True
            enq.save(repo, phone)
            if enq.place and (known := repo.get_property(enq.place)) is not None:
                facts.setdefault("resort", bot._place_label(known))  # the resort code resolved (the eval checks it)
            return self._result(ok, facts, start, enq, note)

        # 1. the place: only one the guest wrote in this chat counts (else it is ignored); unknown -> the owner's
        # location question; switched off -> closed_place; not one of ours -> not_our_place
        place = str(args.get("place") or "").strip()
        ignored = ""
        if place and not self._place_said(place):
            ignored, place = place, ""  # the AI's own guess: never taken
        prop: Property | None = None
        not_ours: dict[str, Any] = {}
        if place:
            if closed := self._closed_place(place):
                return done(True, {"place": place, "available": False, "resort_closed_now": True,
                                   "closed_town": (closed.city or closed.place or closed.name).split(",")[0].strip(),
                                   "towns": self._towns_and()}, SENT_NOTE)
            prop = self._our_place(place)
            if prop is None and self._only_resort() is None:  # a town we have no resort in
                enq.place = None
                return done(True, {"place": place, "ours": False, "towns": self._towns_and()}, SENT_NOTE)
            if prop is None:  # one resort only: the guest is told, and the booking goes on with it
                not_ours = {"place_not_ours": place, "towns": self._towns_and()}
        if prop is None and cur.place:
            prop = repo.get_property(cur.place)
        if prop is None and held:
            prop = repo.get_property(held[0].property_id)
        if prop is None or not prop.active:
            prop = self._only_resort()  # one resort only: the place is set and the location question never goes
        if prop is None or not prop.active:
            enq.place = None
            facts = {"missing": ["place"], "towns": bot._towns_text()}
            if ignored:
                facts["place_ignored"] = f"the guest has not written '{ignored}' in this chat"
            return done(True, facts, SENT_NOTE)
        enq.place = prop.id
        wish, enq.photo_wish = cur.photo_wish, None  # photos asked for before the resort was known

        # the stay as the AI passes it; what it leaves out stays as saved (a field the AI forgets is never lost)
        # the dates: the AI copies only what the guest said ({day, month, year}); code builds them (fix round 3)
        try:
            ci_parts, co_parts = _date_parts(args.get("check_in")), _date_parts(args.get("check_out"))
            ci, full = build_check_in(ci_parts, today) if ci_parts else (None, False)
            base = ci or cur.check_in or today
            co = build_check_out(co_parts, base) if co_parts else None
        except (ValueError, TypeError):
            return done(False, {"problem": "check_in / check_out must be {day, month, year} with only what the guest said"},
                        "Fix the dates and call again.")
        nights = _int(args, "nights")
        if ci and co is None:
            if nights:
                co = ci + timedelta(days=nights)
            elif cur.check_in and cur.check_out:  # a new check-in keeps the number of nights
                co = ci + (cur.check_out - cur.check_in)
        ci = ci or cur.check_in
        co = co or (ci + timedelta(days=nights) if ci and nights else cur.check_out)
        adults = _int(args, "adults", cur.adults)
        children = 0  # children are not asked and not counted (owner, 2026-11-24)
        wanted = _int(args, "rooms", cur.rooms) or 0
        beds_arg = _int(args, "extra_beds", cur.extra_beds) or 0
        picks_arg = args["room_picks"] if args.get("room_picks") is not None else cur.room_picks
        if args.get("exclude_room_types") and args.get("room_picks") is None:
            picks_arg = []
        ids, kept, problem = self._picks(prop, list(picks_arg or []), list(args.get("exclude_room_types") or []))
        if problem:
            return done(False, {"problem": problem}, "Tell the guest nothing invented; ask which room they mean.")

        # 3. a full date that has passed, or a date without a year that passed only days ago (up to 30), is told
        if ci and full and ci < today:  # full = told by build_check_in
            enq.check_in = enq.check_out = None
            return done(True, {"problem": f"the check-in date {_label(ci)} has passed"}, SENT_NOTE)
        if ci and co and (problem := av.check_dates(ci, co, today)):
            return done(False, {"problem": problem}, "Ask the guest for the correct dates in one short line.")

        enq.check_in, enq.check_out, enq.adults, enq.children = ci, co, adults, None
        enq.extra_beds = beds_arg
        enq.rooms = wanted or None
        early = cur.early_checkin if args.get("early_checkin") is None else bool(args["early_checkin"])
        if early and not (prop.early_checkin_time and prop.early_checkin_rate):
            enq.early_checkin = False
            return done(False, {"problem": "early check-in is not offered at this resort"},
                        "Give the owner's numbers with talk_to_owner; never promise it.")
        enq.early_checkin = early

        # 2. dates or persons missing: the owner's form with only the missing lines
        if not (ci and co and adults and adults >= 1):
            facts = {}
            if wish and wish.get("insist"):  # photos asked (and insisted on) before the resort was known go first
                facts = self._photos(enq, prop, str(wish.get("kind") or "default"), str(wish.get("room_type") or ""), lang)
            reason = str(args.get("reason") or "booking")
            got = {"check_in": bool(ci), "check_out": bool(co), "persons": bool(adults), "rooms": wanted}
            return done(True, {**facts, "missing": enq.missing, "form_lines": pp.form_lines(got),
                               "form_reason": reason if reason in pp.FORM_REASONS else "booking"}, SENT_NOTE)

        # 4. everything is known: the room check
        pref, pick = ("", [])
        if len(set(ids)) == 1:
            pref = ids[0]
        elif len(ids) > 1:
            pick = ids
        counted = args.get("room_picks") is not None and any(int(p.get("count") or 0) for p in picks_arg or [])
        if ids and (len(set(ids)) > 1 or counted):
            wanted = len(ids)  # the guest counted the rooms (or mixed room types): that is how many are checked
        bk: dict[str, Any] = {"property_id": prop.id, "check_in": ci.isoformat(), "check_out": co.isoformat(),
                              "adults": adults, "children": 0, "rooms": wanted, "own": phone}
        if pick:
            bk["pick"] = pick
        if pref:
            bk["pref"] = pref
        said_rooms = wanted
        least = av.fewest_rooms(repo.list_room_types(prop.id), adults, children)
        notes: dict[str, Any] = {}  # facts the AI tells the guest along with the room check
        if wanted and least and wanted < least:  # at most 3 per room: the minimum is said and the check goes on
            notes["min_rooms_for_persons"] = least
            wanted = bk["rooms"] = least
        enq.rooms = said_rooms or None  # only what the guest asked for is remembered, never the code's own minimum
        picked_before = (cur.checked or {}).get("pref", "")
        if pref and not bot._fits(prop, bk):  # the picked room cannot take them on these dates
            prt = repo.get_room_type(pref)
            notes["picked_room_not_free"] = prt.name if prt else ""
            pref = ""
            bk.pop("pref", None)
            kept, enq.room_picks = [], []
        else:
            enq.room_picks = kept
        parts, mix = self._plan(prop, bk, wanted)
        if early and mix:
            enq.early_checkin = False
            return done(False, {"problem": "early check-in for a stay over two room types is arranged by the owner"},
                        "Use talk_to_owner for this.")
        if not parts:  # not available: never a dead end (extra-bed offer, or the nearest free dates)
            scratch: dict[str, Any] = {"lang": lang}
            text = bot._no_room_answer(scratch, prop, bk, ci, co, adults, children, wanted)
            facts: dict[str, Any] = {**notes, "available": False, "check_in": ci.isoformat(),
                                     "check_out": co.isoformat()}
            if scratch.get("alt_dates"):
                facts["free_dates"] = scratch["alt_dates"]
            elif scratch.get("suggest_rooms"):
                facts["free_with_extra_beds_in_rooms"] = scratch["suggest_rooms"]
            elif text != t("rooms_none", lang):
                facts["group_bigger_than_all_free_rooms"] = True
            enq.checked = {"available": False, "key": f"none|{prop.id}|{ci.isoformat()}|{co.isoformat()}|{adults}"}
            return done(True, facts, SENT_NOTE)

        quoted = sum(p["rooms"] for p in parts)
        # extra beds (owner, 2026-11-24): never added by themselves. Asked ONCE, only when a room will hold 3 adults; the
        # answer (or a bed the guest asks for on their own) comes back as extra_beds, a number (0 = no)
        shape = f"{adults}|{quoted}"
        beds_given = args.get("extra_beds") is not None
        beds = beds_arg if (beds_given or cur.bed_answered_for == shape) else 0
        if beds and not prop.extra_bed_rate:
            enq.extra_beds = 0
            return done(False, {"problem": "extra beds are not offered at this resort"},
                        "Give the owner's numbers with talk_to_owner; never promise one.")
        threes = av.rooms_with_three(adults, quoted) if prop.extra_bed_rate else 0
        capped = 0  # the answer was more than the rooms that hold 3 adults: capped, and the guest is told the maximum
        if threes and beds > threes:
            beds, capped = threes, threes
        elif beds > quoted:
            enq.extra_beds = 0
            return done(False, {"problem": f"at most one extra bed per room ({quoted} room(s))"},
                        "Call again with a smaller extra_beds.")
        self._apply_beds(parts, beds)
        enq.extra_beds = beds
        asking = bool(threes)
        enq.bed_answered_for = shape if beds_given else (shape if cur.bed_answered_for == shape else None)
        pending = asking and enq.bed_answered_for != shape
        ask_now = pending and cur.bed_asked_for != shape
        enq.bed_asked_for = shape if pending else None
        base = (f"{prop.id}|{ci.isoformat()}|{co.isoformat()}|{adults}|{quoted}|{pref}|{int(early)}|{beds}")
        key = f"{base}|{int(pending)}"
        answered_only = bool((cur.checked or {}).get("extra_bed_pending")) and not pending \
            and (cur.checked or {}).get("base") == base  # only the answer to the extra-bed question came
        price = self._price_of(prop, parts, ci, co, early)
        offered = [p["rt"] for p in parts]
        summary = " + ".join(_count_name(p["rooms"], p["rt"].name) for p in parts)
        enq.checked = {"available": True, "summary": summary, "rooms": quoted, "key": key, "pref": pref,
                       "base": base, "extra_bed_pending": pending, "extra_bed_max": threes,
                       "labels": list((cur.checked or {}).get("labels") or [])}
        facts = {**notes, "available": True, "place": bot._place_label(prop), "check_in": ci.isoformat(),
                 "check_out": co.isoformat(), "nights": (co - ci).days, "adults": adults,
                 "rooms": quoted, "rooms_offered": summary, "extra_beds": sum(p["beds"] for p in parts),
                 "extra_bed_question": pending, "early_checkin": early, "price": price}
        if threes:
            facts["extra_beds_max"] = threes
        if pending and not ask_now:  # asked before for this layout: the AI must not ask again
            facts["extra_bed_question_already_asked"] = True
        last = (cur.checked or {}).get("key", "").split("|")
        changed = (cur.checked or {}).get("key") != key
        same_dates = last[:3] == key.split("|")[:3] or (
            bool(held) and key.split("|")[:3] == [held[0].property_id, held[0].check_in.isoformat(),
                                                  held[0].check_out.isoformat()])  # the dates on the open link
        fewer = av.fewest_rooms(repo.list_room_types(prop.id), adults, children)
        if not wanted and fewer and fewer < quoted and prop.extra_bed_rate:
            facts["fewer_rooms_possible_with_extra_beds"] = fewer
        # room_picked only when the GUEST chose a room (a room type or number in room_picks); a room the code picked by
        # priority (no pick, or only a refused type) never gets it (fix round 3)
        guest_chose = any(str(p.get("room_type") or "").strip() or str(p.get("room_number") or "").strip()
                          for p in (picks_arg or []))
        picked_now = (guest_chose and bool(pref) and picked_before != pref
                      and not (held and {b.room_type_id for b in held} == {pref}))
        if picked_now:
            facts["room_picked"] = offered[0].name
        if capped:  # more beds than rooms that hold 3 adults: capped to the maximum
            facts["extra_beds_capped_to"] = capped

        held_same = False  # is this the stay on the open link?
        if held:
            held_key = (f"{held[0].property_id}|{held[0].check_in.isoformat()}|{held[0].check_out.isoformat()}|"
                        f"{sum(b.adults for b in held)}|{sum(b.rooms for b in held)}")  # the stay ON THE LINK (its own
            # dates too: the new dates here made a date change look like the same stay, fix round 2)
            held_same = (key.split("|")[:5] == held_key.split("|") and {b.room_type_id for b in held} == {rt.id for rt in offered}
                         and ("Early check-in" in (held[0].notes or "")) == early
                         and sum(_beds_on(b) for b in held) == beds)
        closed_link = False
        if held and changed and pending and not held_same:
            # the layout changed after the link and a room will hold 3 adults: the old link is closed AT ONCE, before the
            # extra-bed answer ('old link closed on any change'); the name and mobile are kept for the new link
            stay = bot._held_stay(phone) or {}
            guest = repo.get_guest(phone)
            enq.contact = {"name": stay.get("guest_name") or (guest.name if guest else ""),
                           "mobile": stay.get("mobile") or "same"}
            bot.close_old_holds(phone, set())
            held, closed_link = [], True
            enq.booking_id, enq.link_open = None, False
        if held and changed and not pending:  # a change after the payment link: the old link is closed, a new link goes
            if held_same:
                return done(True, facts, UNCHANGED_NOTE)  # the same stay as the open link
            return self._relink(enq, prop, bk, parts, mix, held, lang, facts, start)
        if enq.contact and changed and not pending and not held:
            # the answer came after the link was closed: the new summary and link go at once (name and mobile are known)
            return self._relink(enq, prop, bk, parts, mix, [], lang, facts, start, contact=enq.contact)
        if not changed:
            return done(True, facts, UNCHANGED_NOTE)
        if answered_only:  # only the answer to the extra-bed question came: nothing new to show
            return done(True, facts, SENT_NOTE)
        shown = list((cur.checked or {}).get("labels") or [])  # the room labels the last room check sent
        # new dates: the glimpse and the general photos, then each room's label and photos; only persons, rooms or the
        # room type changed: a new room's label and photos
        self._stay_photos(enq, prop, parts, bool(mix), price, not same_dates, shown)
        return done(True, facts, SENT_NOTE + (" The old payment link is closed: a new summary and link follow the guest's "
                                              "answer to the extra-bed question." if closed_link else ""))

    def _stay_photos(self, enq: Enquiry, prop: Property, parts: list[dict[str, Any]], mixed: bool,
                     price: dict[str, Any], first_time: bool, shown: list[str] | None = None) -> None:
        """The owner's order after a room check (rule 2): the glimpse, the general photos not sent yet, then FOR EACH
        offered room its label and photos. Only the room photos when just the persons or rooms changed on the same
        dates. A label the last check (`shown`) or this conversation already sent goes again only with new photos; the
        labels are kept in enq.checked["labels"]."""
        shown = list(shown or [])
        sent = pp.sent_refs(self.bot, self.phone, self.out)
        if first_time:
            if prop.intro and enq.intro_sent != prop.id:
                nights = (enq.check_out - enq.check_in).days
                per = price.get("per_room_per_night") or round(price["total"] / max(1, nights * sum(p["rooms"] for p in parts)))
                self.out.text("glimpse", prop.intro.replace("{price}", _money(per)).replace("{room}", parts[0]["rt"].name))
                enq.intro_sent = prop.id
            if general := pp.general_photos(prop, sent):
                self.out.photos(f"photos:{len(general)}", general)
                sent |= set(general)
        for part in parts:  # FOR EACH offered room: its label (even with no photo), then its photos (fix round 3)
            for label, refs in pp.offered_sets(self.bot, part["rt"], enq, part["rooms"], sent, self.phone):
                if not refs and (label in shown or pp.already_said(self.bot, self.phone, self.out, label)):
                    continue  # this room was labelled already and no new photo follows (a re-check of the same rooms)
                self.out.text("room_label", label)
                shown.append(label)
                if refs:
                    self.out.photos(f"photos:{len(refs)}", refs)
                    sent |= set(refs)
        enq.checked = {**(enq.checked or {}), "labels": shown}

    # ------------------------------------------------------------------ send_photos

    def _photos(self, enq: Enquiry, prop: Property, kind: str, room_type: str, lang: str) -> dict[str, Any]:
        """Queue the photos the owner's rules give for this kind, or the owner's text when there is nothing to send. A
        `problem` key in the facts means the AI named a room that does not exist."""
        bot, phone = self.bot, self.phone
        sent = pp.sent_refs(bot, phone, self.out)
        plan = pp.plan(bot, enq, prop, kind, room_type, sent, phone)
        if plan.problem == "unknown_room":
            return {"problem": f"there is no room type '{room_type}'; the room types are in the FACTS"}
        if plan.problem == "photos_sent" and not sent:
            return {"photos": 0, "info": "no photo has been uploaded for this resort yet"}
        if plan.problem:  # photos_sent | no_other_room | room_not_free | other_free
            if plan.problem == "other_free":
                self._send_free(plan.room, lang)
            return {"photos": 0, **PHOTO_PROBLEMS[plan.problem](plan.room)}
        facts: dict[str, Any] = {}
        if plan.also_free and plan.steps and plan.steps[0][0] is not None:  # another free room: shown, never switched
            facts["also_free_for_your_dates"] = plan.steps[0][0].name
            self._send_free(plan.steps[0][0].name, lang)
        count = pp.queue_steps(self.out, plan.steps, bot, enq, phone)
        return {**facts, "photos": count, "kind": kind, "rooms": [rt.name for rt, _ in plan.steps if rt is not None]}

    def _send_free(self, room: str, lang: str) -> None:
        """'Yes, X is also free for your dates.' before its label and photos: a code-checked fact, so code says it."""
        self.out.text("also_free", t("also_free", lang, room=room))

    def send_photos(self, args: dict[str, Any]) -> dict[str, Any]:
        bot, phone, repo = self.bot, self.phone, self.bot.repo
        start = len(self.out.keys)
        enq = Enquiry.load(repo, phone)
        style = _style(args, enq.language)
        lang = I18N_LANG[style]
        enq.language = style
        kind, room_type = str(args.get("kind") or "default"), str(args.get("room_type") or "").strip()
        if kind not in pp.KINDS:
            return self._result(False, {"problem": f"kind must be one of {', '.join(pp.KINDS)}"}, start, enq,
                                "Call again with a valid kind.")
        held = bot._open_hold(phone)
        if held:  # the payment link is out: that is the booking step (nothing is asked again after the photos)
            enq.booking_id, enq.link_open = held[0].id, True
        enq.started = True
        prop = repo.get_property(enq.place) if enq.place else None
        if prop is None and held:
            prop = repo.get_property(held[0].property_id)
        if prop is None or not prop.active:
            if (prop := self._only_resort()) is not None:  # one resort only: never the location question
                enq.place = prop.id
        purpose, insist = str(args.get("purpose") or "consider"), bool(args.get("insist"))
        if prop is None or not prop.active:  # no resort yet: the location question first, the request is kept (rule 6)
            enq.photo_wish = {"kind": kind, "room_type": room_type, "insist": insist}
            enq.save(repo, phone)
            return self._result(True, {"photos": 0, "missing": ["place"], "towns": bot._towns_text()}, start, enq,
                                "Ask which location with reply(); the photos follow once the guest names the resort.")
        view = Enquiry(place=enq.place, room_picks=enq.room_picks, language=enq.language)  # photos with no dates
        note = SENT_NOTE
        if enq.paid or self._paid_stay(enq):
            # after payment photos are still always allowed: planned without dates, so the guest's own paid booking
            # never makes their room look 'not free' (fix round 4)
            facts = self._photos(view, prop, kind, room_type, lang)
        elif not (enq.check_in and enq.check_out):  # owner, 2026-11-26: no dates -> the form first, photos only if
            if not insist:                         # the guest insists
                got = {"check_in": bool(enq.check_in), "check_out": bool(enq.check_out), "persons": bool(enq.adults),
                       "rooms": enq.rooms or 0}
                enq.save(repo, phone)
                return self._result(True, {"photos": 0, "dates_missing": True, "form_lines": pp.form_lines(got),
                                           "form_reason": "availability"}, start, enq,
                                    "No photos yet: reply with the form (form_reason availability). Send photos only "
                                    "if the guest insists (insist=true).")
            facts = {**self._photos(view, prop, kind, room_type, lang), "dates_missing": True}
            note = ("Photos sent. Add ONE short reminder (max 10 words), e.g. 'Please share your dates to check "
                    "availability.' Never the full form again.")
        elif purpose == "see":  # dates known, they only want to look: no availability check
            facts = {**self._photos(view, prop, kind, room_type, lang), "availability_checked": False}
            note = "Photos only: availability was NOT checked, so never say free or available."
        else:  # consider: another room / switching / is it free, on THEIR dates (their own hold is not counted)
            facts = self._photos(enq, prop, kind, room_type, lang)
        enq.save(repo, phone)
        return self._result("problem" not in facts, facts, start, enq, note)

    def _paid_stay(self, enq: Enquiry) -> bool:
        """The guest has a PAID booking for the enquiry's dates (or any upcoming one when no dates are known)."""
        today = self.bot._today()
        for b in self.bot.repo.list_bookings(phone=self.phone, limit=10):
            if b.status == "paid" and b.check_out >= today and (
                    not enq.check_in or (b.check_in < (enq.check_out or enq.check_in) and b.check_out > enq.check_in)
                    or b.check_in == enq.check_in):
                return True
        return False

    def _relink(self, enq: Enquiry, prop: Property, bk: dict[str, Any], parts: list[dict[str, Any]], mix: Any,
                held: list[Any], lang: str, facts: dict[str, Any], start: int,
                contact: dict[str, Any] | None = None) -> dict[str, Any]:
        """The stay changed after the payment link went out: a new booking and link are made and the old one is closed
        (close_old_holds); the name and mobile come from the held booking, or from `contact` when the old link was already
        closed (a layout change that waited for the extra-bed answer)."""
        bot = self.bot
        stay = {"guest_name": contact["name"], "mobile": contact["mobile"]} if contact else (bot._held_stay(self.phone) or {})
        guest = bot.repo.get_guest(self.phone)
        name = stay.get("guest_name") or (guest.name if guest else "")
        ctx, made, failure = self._make_booking(enq, prop, bk, parts, mix, lang, name, stay.get("mobile") or "same", start,
                                                early=enq.early_checkin)
        if failure is not None:
            return failure
        self.out.text("summary", ctx.summary_message)
        self.out.text("pay", ctx.pay_message)
        enq.booking_id, enq.link_open, enq.contact = made["booking_id"], True, None
        enq.save(bot.repo, self.phone)
        return self._result(True, {**facts, "old_link_closed": True, "new_summary_and_link_sent": True}, start, enq,
                            SENT_NOTE)

    def _make_booking(self, enq: Enquiry, prop: Property, bk: dict[str, Any], parts: list[dict[str, Any]], mix: Any,
                      lang: str, name: str, mobile: str, start: int, early: bool = False
                      ) -> tuple[_Turn, dict[str, Any], dict[str, Any] | None]:
        """The existing prepare / create code makes the booking and the payment link. The turn is quiet: nothing is read
        from the old chat log and nothing is sent past the outbox; the guest's texts and the staff alert come back through
        it. Returns (turn, result, a failure result for the AI or None)."""
        bot = self.bot
        if mix:
            bk["mix"] = [{"room_type_id": p["rt"].id, "rooms": p["rooms"], "beds": p["beds"], "total": p["total"],
                          "advance": p["advance"]} for p in parts]
        scratch = {"lang": lang, "style": enq.language, "turn": 1, "bk": bk, "property_id": prop.id}
        ctx = _Turn(self.phone, scratch, text="", quiet=True)
        beds = sum(p["beds"] for p in parts)
        prep = bot._tool_prepare(ctx, {"rooms": sum(p["rooms"] for p in parts), **({"extra_beds": beds} if beds else {}),
                                       **({"early_checkin": True} if early else {})})
        if "error" in prep:
            self._queue_quiet(ctx, lang, prop)  # e.g. the last rooms were just taken: the owner's text
            enq.save(bot.repo, self.phone)
            return ctx, prep, self._result(False, {"problem": prep["error"]}, start, enq, "Tell the guest nothing invented.")
        made = bot._tool_create(ctx, {"guest_name": name, "mobile": mobile})
        if "error" in made or ctx.handed_off:
            self._queue_quiet(ctx, lang, prop)  # the sold-out text, or the owner's numbers when the link could not be made
            enq.save(bot.repo, self.phone)
            note = ("The link could not be made and the team is alerted: give the guest the team's numbers with "
                    "reply(). Do not promise anything." if ctx.handed_off else
                    "Ask the guest for the missing detail with reply(). Do not guess it.")
            return ctx, made, self._result(False, {"problem": made.get("error", "the payment link could not be made"),
                                                   "numbers": self._numbers(prop)}, start, enq, note)
        return ctx, made, None

    def _staff(self, reason: str, property_id: str | None, lang: str) -> None:
        """A short summary for the team, through the outbox (a dry run only records it)."""
        text = self.bot.handoff_summary(self.phone, {"lang": lang}, reason)
        self.out.staff("staff:alert", text, self.bot.staff_numbers(property_id))

    def _queue_quiet(self, ctx: _Turn, lang: str, prop: Property | None = None) -> None:
        """What the existing prepare / create code left in a quiet turn: only its staff alerts go (the guest's texts
        become the failure result's facts; the AI tells the guest)."""
        for reason in ctx.staff:
            self._staff(reason, prop.id if prop else None, lang)

    # ------------------------------------------------------------------ get_price

    def get_price(self, args: dict[str, Any]) -> dict[str, Any]:
        """The price of the known stay (or the stay on the open payment link) from the dashboard rates now. It only
        answers: the Enquiry, the booking and the payment link are never changed, and the room is never switched."""
        bot, phone = self.bot, self.phone
        repo = bot.repo
        start = len(self.out.keys)
        enq = Enquiry.load(repo, phone)
        style = _style(args, enq.language)
        lang = I18N_LANG[style]
        held = bot._held_stay(phone)
        if enq.place and enq.check_in and enq.check_out and enq.adults:
            prop = repo.get_property(enq.place)
            ci, co, adults, children = enq.check_in, enq.check_out, enq.adults, 0
            wanted, pref, early = enq.rooms or 0, "", enq.early_checkin
            if len({p.get("room_type") for p in enq.room_picks}) == 1:
                rt0 = self._room(repo.list_room_types(prop.id), enq.room_picks[0]["room_type"]) if prop else None
                pref = rt0.id if rt0 else ""
        elif held:  # the payment link is out: its stay is what we know
            prop = repo.get_property(held["property_id"])
            ci, co = date.fromisoformat(held["check_in"]), date.fromisoformat(held["check_out"])
            adults, children, wanted = held["adults"], 0, int(held.get("rooms") or 0)
            pref = held["room_type_ids"][0] if len(set(held["room_type_ids"])) == 1 else ""
            early = any("Early check-in" in (b.notes or "") for b in bot._open_hold(phone))
        else:  # no stay yet: what is missing, form_reason price ('To check the price ...', owner 2026-11-26)
            result = self.update_enquiry({"language": style, "reason": "price"})
            result["sent"] = self.out.keys[start:]
            return result
        if prop is None:
            return self._result(False, {"problem": "unknown resort"}, start, enq, "Call update_enquiry first.")
        named = str(args.get("room_type") or "").strip()
        if named:  # the price of THAT room for the same stay; only a question, never a switch
            rt = self._room(repo.list_room_types(prop.id), named) or self.bot._room_named(
                named, repo.list_room_types(prop.id))
            if rt is None:
                return self._result(False, {"problem": f"there is no room type '{named}'"}, start, enq,
                                    "The room types are in the FACTS.")
            pref = rt.id
        bk = {"property_id": prop.id, "check_in": ci.isoformat(), "check_out": co.isoformat(), "adults": adults,
              "children": children, "rooms": wanted, "own": phone}
        if pref:
            bk["pref"] = pref
        parts, _ = self._plan(prop, bk, wanted)
        if not parts and wanted:
            parts, _ = self._plan(prop, {**bk, "rooms": 0}, 0)
        if parts:
            self._apply_beds(parts, enq.extra_beds if (enq.place and enq.check_in and enq.adults)
                             else sum(_beds_on(b) for b in bot._open_hold(phone)))
        if not parts:
            rt = repo.get_room_type(pref) if pref else None
            return self._result(True, {"available": False, **({"room_not_free": rt.name} if rt is not None else {})},
                                start, enq, SENT_NOTE)
        price = self._price_of(prop, parts, ci, co, early and len(parts) == 1)
        beds = sum(p["beds"] for p in parts)
        room = " + ".join(_count_name(p["rooms"], p["rt"].name) for p in parts) if len(parts) > 1 else parts[0]["rt"].name
        rooms = sum(p["rooms"] for p in parts)
        facts = {"room_type": room, "rooms": rooms, "extra_beds": beds, "dates": av.fmt_range(ci, co),
                 "nights": (co - ci).days, "price": price}
        return self._result(True, facts, start, enq, "Only a price answer: nothing is booked or changed. Give the "
                            "amounts with reply() (per room per night when there is one, and the total).")

    # ------------------------------------------------------------------ shared by the tools below

    def _ctx(self, args: dict[str, Any]) -> tuple[int, Enquiry, str, str]:
        """(outbox position, the saved Enquiry with this call's language, language style, i18n language code)."""
        enq = Enquiry.load(self.bot.repo, self.phone)
        style = _style(args, enq.language)
        enq.language = style
        return len(self.out.keys), enq, style, I18N_LANG[style]

    def _the_resort(self, enq: Enquiry, held: list[Any]) -> Property | None:
        """The resort this guest is dealing with: the Enquiry's, the one on their open link, or the only open resort."""
        repo = self.bot.repo
        prop = repo.get_property(enq.place) if enq.place else None
        if prop is None and held:
            prop = repo.get_property(held[0].property_id)
        if prop is None and len(places := self.bot._places()) == 1:
            prop = places[0]
        return prop if prop is not None and prop.active else None

    # ------------------------------------------------------------------ create_booking

    def create_booking(self, args: dict[str, Any]) -> dict[str, Any]:
        """The booking and its payment link, from the stay the room check found available. The name comes only from the
        argument (empty is allowed); mobile 'same' is the WhatsApp number."""
        bot, phone, repo = self.bot, self.phone, self.bot.repo
        start, enq, style, lang = self._ctx(args)
        prop = repo.get_property(enq.place) if enq.place else None
        if prop is None or not (enq.check_in and enq.check_out and enq.adults) or not (enq.checked or {}).get("available"):
            return self._result(False, {"problem": "no available stay has been checked yet"}, start, enq,
                                "Call update_enquiry with the stay first; the owner's messages for it are sent for you.")
        if (enq.checked or {}).get("extra_bed_pending"):  # the summary and the link wait for the answer
            return self._result(False, {"problem": "the extra-bed question is not answered yet"}, start, enq,
                                "'Do you need an extra bed?' was already sent. When the guest answers, call update_enquiry "
                                "with extra_beds (a number for yes, 0 for no), then create_booking.")
        ids, _, problem = self._picks(prop, list(enq.room_picks or []), [])
        if problem:
            return self._result(False, {"problem": problem}, start, enq, "Ask the guest which room they mean.")
        pref = ids[0] if len(set(ids)) == 1 else ""
        wanted = len(ids) if len(set(ids)) > 1 else (enq.rooms or 0)
        bk: dict[str, Any] = {"property_id": prop.id, "check_in": enq.check_in.isoformat(),
                              "check_out": enq.check_out.isoformat(), "adults": enq.adults,
                              "children": 0, "rooms": wanted, "own": phone}
        if len(set(ids)) > 1:
            bk["pick"] = ids
        if pref:
            bk["pref"] = pref
        least = av.fewest_rooms(repo.list_room_types(prop.id), enq.adults, 0)
        if wanted and least and wanted < least:
            wanted = bk["rooms"] = least
        parts, mix = self._plan(prop, bk, wanted)
        if parts:
            self._apply_beds(parts, enq.extra_beds if prop.extra_bed_rate else 0)
        if not parts:  # the rooms went to someone else since the check
            enq.checked = None
            enq.save(repo, phone)
            return self._result(False, {"available": False}, start, enq, SENT_NOTE)
        self._price_of(prop, parts, enq.check_in, enq.check_out, enq.early_checkin)  # fills each part's total
        name = str(args.get("guest_name") or "").strip()
        ctx, made, failure = self._make_booking(enq, prop, bk, parts, mix, lang, name, str(args.get("mobile") or "").strip(),
                                                start, early=enq.early_checkin)
        if failure is not None:
            return failure
        self.out.text("summary", ctx.summary_message)
        self.out.text("pay", ctx.pay_message)
        enq.booking_id, enq.link_open, enq.photo_wish, enq.contact = made["booking_id"], True, None, None
        enq.save(repo, phone)
        return self._result(True, {"summary_and_link_sent": True}, start, enq,
                            "Write nothing more unless the guest asked something else. Never say it is booked or "
                            "confirmed: it is confirmed only when the payment is received.")

    # ------------------------------------------------------------------ check_payment

    def check_payment(self, args: dict[str, Any]) -> dict[str, Any]:
        """Ask the payment provider about the guest's OWN link, at most once every 15 seconds. Only the provider decides."""
        bot, phone, repo = self.bot, self.phone, self.bot.repo
        start, enq, style, lang = self._ctx(args)
        links = [b for b in repo.list_bookings(status="hold", phone=phone, limit=20)
                 if b.payment_link_id and not (b.group_id and b.group_id != b.id)]
        if not links:
            latest = next(iter(repo.list_bookings(phone=phone, limit=1)), None)
            if latest is not None and latest.status == "paid":
                return self._result(True, {"already_paid": True}, start, enq,
                                    "This booking is already paid and its confirmation was sent: say nothing more, or one "
                                    "warm line.")
            return self._result(True, {"checked": False, "reason": "no open payment link"}, start, enq,
                                "There is nothing to check: follow next_step (the guest has no payment link yet).")
        last = bot.__dict__.setdefault("_pay_checked", {})
        if time.monotonic() - last.get(phone, -1e9) < bot.PAY_CHECK_GAP_S:  # just checked: the answer would be the same
            return self._result(True, {"paid": False, "payment_received": False}, start, enq, SENT_NOTE)
        last[phone] = time.monotonic()
        for b in links:
            try:
                status = bot.payments.link_status(b.payment_link_id)
            except Exception as exc:  # the provider being slow must never stop the guest being answered
                log.warning("Payment check failed: %s", exc)
                continue
            if not status.get("paid"):
                continue
            if self.out.dry_run:  # a dry run never marks anything paid or sends a confirmation
                self.out.marker("confirmed")
                return self._result(True, {"paid": True, "applied": False}, start, enq,
                                    "Dry run: the payment was found but nothing was applied.")
            outcome = bot.mark_paid(booking_id=b.id, payment_id=status.get("payment_id", ""), out=self.out)
            if outcome == "paid":
                enq.paid, enq.link_open = True, False
                enq.save(repo, phone)
                return self._result(True, {"paid": True}, start, enq,
                                    "The confirmation went out by itself: write nothing more.")
            if outcome == "refund_needed":  # paid after the hold ran out and the rooms are gone
                return self._result(True, {"paid": True, "rooms_gone": True,
                                           "numbers": self._numbers(repo.get_property(b.property_id))}, start, enq,
                                    "Give the team's numbers with reply(). Never promise a refund.")
            return self._result(True, {"paid": True, "already_confirmed": True}, start, enq,
                                "The booking was already confirmed: say nothing more.")
        return self._result(True, {"paid": False, "payment_received": False}, start, enq,
                            SENT_NOTE + " Never say it is confirmed.")

    # ------------------------------------------------------------------ send_payment_link

    def send_payment_link(self, args: dict[str, Any]) -> dict[str, Any]:
        """The payment link again, only while the hold is live. hold -> the owner's 'pay now' line; others -> the link."""
        bot, phone, repo = self.bot, self.phone, self.bot.repo
        start, enq, style, lang = self._ctx(args)
        reason = str(args.get("reason") or "")
        if reason not in ("lost", "hold", "not_paid_yet"):
            return self._result(False, {"problem": "reason must be lost, hold or not_paid_yet"}, start, enq,
                                "Call again with a valid reason.")
        now = bot.clock.now()
        live = [b for b in repo.list_bookings(phone=phone, status="hold", limit=5)
                if b.payment_link_url and b.hold_expires_at and b.hold_expires_at > now]
        if not live:
            latest = next(iter(repo.list_bookings(phone=phone, limit=1)), None)
            if latest is not None and latest.payment_link_id and latest.status in ("hold", "expired"):
                return self._renew(latest, enq, style, start)
            return self._result(False, {"open_link": False}, start, enq,
                                "No payment link is open for this guest: follow next_step.")
        b = live[0]
        if reason == "hold":
            self.out.text("hold_pay", t("hold_pay", lang, url=b.payment_link_url))
        else:
            self.out.text("pay", pay_text(b, lang, now))
        enq.booking_id, enq.link_open = b.id, True
        return self._result(True, {"link_sent": True}, start, enq,
                            "Write nothing more unless the guest asked something else. Never explain the hold or when "
                            "the link expires.")

    def _renew(self, old: Any, enq: Enquiry, style: str, start: int) -> dict[str, Any]:
        """The guest wants the link again but its hold ran out (fix round 2): the same stay is checked again and, when the
        rooms are still free, a new summary and link go at once with the old name and mobile (update_enquiry's relink).
        Rooms gone -> the owner's no-room text. The guest is never told about the hold or its expiry."""
        repo = self.bot.repo
        parts = [b for b in repo.list_bookings(phone=self.phone, limit=10)
                 if b.id == old.id or (old.payment_link_id and b.payment_link_id == old.payment_link_id)]
        if not (enq.place and enq.check_in and enq.check_out and enq.adults):  # the stay comes from the old link
            enq.place, enq.check_in, enq.check_out = old.property_id, old.check_in, old.check_out
            enq.adults, enq.extra_beds = sum(b.adults for b in parts), sum(_beds_on(b) for b in parts)
        enq.contact = {"name": old.guest_name or "", "mobile": _mobile_on(old) or "same"}
        ci, co = enq.check_in.isoformat(), enq.check_out.isoformat()
        enq.checked = {**(enq.checked or {}), "key": f"{enq.place}|{ci}|{co}|renew"}  # same dates, but a new check runs
        enq.booking_id, enq.link_open = None, False
        enq.save(repo, self.phone)
        result = self.update_enquiry({"language": style})
        result["sent"] = self.out.keys[start:]
        return result

    def send_map(self, args: dict[str, Any]) -> dict[str, Any]:
        """The resort's Google Maps link, then the booking step still open."""
        bot = self.bot
        start, enq, style, lang = self._ctx(args)
        held = bot._open_hold(self.phone)
        if held:
            enq.booking_id, enq.link_open = held[0].id, True
        prop = self._the_resort(enq, held)
        if prop is None:
            return self._result(False, {"problem": "no resort is known yet", "towns": bot._towns_text()}, start, enq,
                                "Ask which location with reply().")
        if not prop.map_url:
            return self._result(True, {"map": False, "numbers": self._numbers(prop)}, start, enq,
                                "No map link is saved for this resort: say so in one line with the team's numbers.")
        self.out.text("map", f"\U0001F4CD {prop.name}\n{prop.map_url}")
        enq.save(bot.repo, self.phone)
        return self._result(True, {"map": True}, start, enq, SENT_NOTE)

    # ------------------------------------------------------------------ my_bookings (read only)

    def my_bookings(self, args: dict[str, Any]) -> dict[str, Any]:
        """The guest's own recent bookings. Never a booking id, never a payment link (the AI uses send_payment_link)."""
        bot = self.bot
        start, enq, style, lang = self._ctx(args)
        today = bot._today()
        rows = []
        for b in bot.repo.list_bookings(phone=self.phone, limit=5):
            prop = bot.repo.get_property(b.property_id)
            row: dict[str, Any] = {"place": bot._place_label(prop) if prop else "", "check_in": b.check_in.isoformat(),
                                   "check_out": b.check_out.isoformat(), "rooms": b.rooms,
                                   "status": status_name(b.status, "en"), "total": b.total, "paid": b.paid_amount,
                                   "balance_at_property": b.balance}
            if b.status == "hold" and b.payment_link_url:
                row["payment_link_open"] = bool(b.hold_expires_at and b.hold_expires_at > bot.clock.now())
            if b.status == "paid" and b.check_in >= today and (terms := cancellation_terms(prop, b, today)):
                row["if_cancelled_today"] = terms  # the owner's policy, worked out in code
            rows.append(row)
        facts = {"bookings": rows} if rows else {"bookings": [], "info": "this guest has no booking yet"}
        return self._result(True, facts, start, enq,
                            "Read only. Say only what was asked. A cancellation: state only the charge in if_cancelled_today "
                            "and tell them to contact the owner directly to confirm (talk_to_owner).")

    # ------------------------------------------------------------------ change_name

    def change_name(self, args: dict[str, Any]) -> dict[str, Any]:
        """A name correction: the name changes on the guest's open and upcoming bookings; nothing restarts."""
        bot, repo = self.bot, self.bot.repo
        start, enq, style, lang = self._ctx(args)
        raw = "".join(ch for ch in str(args.get("name") or "") if ch.isascii() and (ch.isalpha() or ch in " .'")).strip()[:60]
        if not raw:
            return self._result(False, {"problem": "give the new name in English letters"}, start, enq,
                                "Ask the guest to spell the name.")
        name = " ".join(w[:1].upper() + w[1:] for w in raw.split())
        today, changed = bot._today(), 0
        for b in repo.list_bookings(phone=self.phone, limit=10):
            if b.status in ("hold", "paid", "checked_in") and b.check_out >= today:
                b.guest_name = name
                repo.save_booking(b)
                changed += 1
        guest = repo.get_guest(self.phone)
        if guest is not None:
            guest.name = name
            repo.upsert_guest(guest)
        return self._result(True, {"name_changed_to": name, "bookings_changed": changed}, start, enq, SENT_NOTE)

    # ------------------------------------------------------------------ talk_to_owner

    def talk_to_owner(self, args: dict[str, Any]) -> dict[str, Any]:
        """The team gets a short summary (with the AI's staff_note). The guest gets nothing from here: the AI writes the
        reply itself (e.g. the team's numbers from facts). The agent keeps answering afterwards."""
        bot = self.bot
        start, enq, style, lang = self._ctx(args)
        reason = str(args.get("reason") or "").strip()
        note = str(args.get("staff_note") or "").strip()[:200]
        if reason not in OWNER_REASONS:  # free text instead of a reason: it goes to the staff
            reason, note = "other", (note or reason)[:200]
        prop = self._the_resort(enq, bot._open_hold(self.phone))
        staff_line = note if reason == "other" and note else (f"{OWNER_REASONS[reason]}: {note}" if note
                                                               else OWNER_REASONS[reason])
        self._staff(staff_line, prop.id if prop else None, lang)
        return self._result(True, {"team_told": True, "reason": reason, "numbers": self._numbers(prop)}, start, enq,
                            "Nothing was sent to the guest: answer with reply(). Never agree to anything yourself, "
                            "never promise a call back or say the team will contact them.")

    # ------------------------------------------------------------------ recall_old_chat (read only)

    def recall_old_chat(self, args: dict[str, Any]) -> dict[str, Any]:
        """What was said in this guest's EARLIER conversations (a 12 h gap, a restart or a confirmed booking ends one). Links are hidden."""
        bot = self.bot
        start, enq, style, lang = self._ctx(args)
        rows = bot.repo.list_chat(self.phone, limit=80)
        older = conversation.since_restart(bot, self.phone, rows[:conversation.start_index(rows)])
        if not older:
            return self._result(True, {"messages": [], "info": "there is no earlier chat with this guest"}, start, enq,
                                "Read only.")
        words = [w for w in str(args.get("query") or "").casefold().split() if len(w) >= 3]
        hits = [m for m in older if any(w in m.text.casefold() for w in words)] if words else []
        chosen = (hits or older)[-30:]
        lines = [f"{m.at.strftime('%d %b')} {'Guest' if m.direction == 'in' else 'Bot'}: "
                 + " ".join("[link]" if w.startswith("http") else w for w in m.text.split())[:300] for m in chosen]
        return self._result(True, {"messages": lines}, start, enq,
                            "Earlier chat, oldest first, links hidden. Never paste a link: use send_payment_link (only "
                            "while the hold is live). Answer only what the guest asked.")

    # ------------------------------------------------------------------ set_language

    def set_language(self, args: dict[str, Any]) -> dict[str, Any]:
        """'English please': that language is locked for the rest of the conversation (a restart clears it)."""
        start, enq, style, lang = self._ctx(args)
        if self.lang is not None:
            style = self.lang.set_lock(style)
        enq.language = style
        enq.save(self.bot.repo, self.phone)
        return self._result(True, {"language": style}, start, enq,
                            f"From now on reply in {style} only, for the rest of this conversation.")

    # ------------------------------------------------------------------ calling a tool by name

    def call(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        """Run one tool the AI asked for. An unknown name or a crash is a normal result, never an exception."""
        handlers = {"update_enquiry": self.update_enquiry, "get_price": self.get_price, "send_photos": self.send_photos,
                    "send_map": self.send_map, "create_booking": self.create_booking,
                    "check_payment": self.check_payment, "send_payment_link": self.send_payment_link,
                    "my_bookings": self.my_bookings, "change_name": self.change_name,
                    "talk_to_owner": self.talk_to_owner, "recall_old_chat": self.recall_old_chat,
                    "set_language": self.set_language}
        start, enq = len(self.out.keys), Enquiry.load(self.bot.repo, self.phone)
        if self.lang is not None:  # the AI's language for this call, else this turn's (never an older conversation's)
            args = {**(args or {}), "language": self.lang.take((args or {}).get("language"))}
        handler = handlers.get(name)
        if handler is None:
            return self._result(False, {"problem": f"unknown tool {name}"}, start, enq, "Use one of the listed tools.")
        if name in ONCE_TOOLS and name in self.ran:  # a sending tool runs at most once per turn (fix round 2)
            return self._result(True, {"already_done": True}, start, enq, ONCE_NOTE)
        self.ran.append(name)
        try:
            return handler(args or {})
        except Exception as exc:  # a bad tool call must never leave the guest unanswered
            log.exception("Tool %s failed", name)
            return self._result(False, {"problem": f"internal error: {type(exc).__name__}"}, start, enq,
                                "Say nothing invented; use talk_to_owner.")
