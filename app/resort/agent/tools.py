"""The agent's 11 tools (docs/AI_AGENT_DESIGN.md section 3): update_enquiry, get_price, send_photos, send_map,
create_booking, check_payment, send_payment_link, my_bookings, change_name, talk_to_owner, recall_old_chat.

The AI passes structured arguments; code does everything else. Nothing here reads or matches the guest's own words:
no regex, no keyword list. A tool compares the stay the AI passed with the saved Enquiry, runs the room check with the
existing availability and pricing code (availability.py, the BookingBot helpers, the repository), and puts the owner's
fixed texts (i18n.py) into the outbox in the passed language.

Every result has the same shape: {ok, facts, sent, next_step, note}.
  facts      what the AI may use in its own words (prices only inside facts["price"], and only if the guest asked)
  sent       the outbox keys this call queued: the AI must not write those messages again
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
from app.resort.agent.outbox import Outbox, near_same
from app.resort.agent.state import LANGUAGES, Enquiry, _day
from app.resort.chat import _Turn, _count_name, _money, close_word
from app.resort.i18n import status_name, t
from app.resort.models import Property, RoomType
from app.resort.refunds import cancellation_terms

log = logging.getLogger(__name__)

I18N_LANG = {"en": "en", "manglish": "mn", "tanglish": "tn", "hinglish": "hn"}

STEP_SHOWN = "step_shown"  # an outbox marker: the open step is the last message already, the turn adds it no more
ALREADY_SENT = "Already sent to the guest. Do not repeat it."  # the start of every note when a tool sent messages
MID_HI = "Hi."  # a mid-conversation hello with nothing to add (same in every language, like the owner's greet)
SENT_NOTE = ("Write NOTHING unless the guest also asked something these messages did not answer; then answer only that, "
             "in one short sentence.")
UNCHANGED_NOTE = ("Nothing new to send: the stay is unchanged. Answer only what the guest asked, in one short line.")

# talk_to_owner(reason): each reason has its fixed owner text (i18n key) and a line for the team (None = no alert)
OWNER_REASONS: dict[str, tuple[str, str | None]] = {
    "pay_terms": ("pay_terms", "guest wants other payment terms (smaller advance / cash / pay at the property / pay later)"),
    "guest_called": ("guest_called", None),  # the guest already reached the owner: no alert
    "cannot_answer": ("handoff", None),  # fix round 2: the guest gets the team's numbers, no staff alert
    "call_request": ("handoff", None),
    "change_or_cancel_paid": ("handoff", "the guest wants to change or cancel a PAID booking"),
    "other": ("handoff", "the guest needs the owner"),
}

LANGUAGE_FIELD = {"type": "string", "enum": list(LANGUAGES),
                  "description": "the language style of the guest's latest message: en = English; manglish = Malayalam "
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
ONCE_TOOLS = ("send_payment_link", "send_photos", "create_booking", "check_payment", "talk_to_owner", "send_map",
              "repeat_step", "greet")
ONCE_NOTE = "Already done this turn: nothing more was sent. Do not call it again and do not repeat it."
# talk_to_owner reasons after which the guest gets ONLY the fixed text: the AI's own line and the open step are dropped
QUIET_REASONS = ("cannot_answer", "call_request", "other", "guest_called")
# cannot_answer is not here (fix round 3): a question not in FACTS gets the handoff text, THEN the open booking step
NO_STEP_REASONS = ("call_request", "other")

# Descriptions are short (one example each) and do not repeat the system prompt: they are sent on every call (cached).
TOOL_SCHEMAS = [
    ToolSpec(
        "update_enquiry",
        "Any wish to book, see rooms or know availability (even 'I need a room'), and anything the guest says about "
        "their stay (place, dates, nights, adults, rooms, a room type or number, 'make it 5 persons'). "
        "A place or resort name on its own ('Alleppey', 'kanthaloor', even misspelt) IS booking information → call "
        "update_enquiry(place=...). Pass the FULL stay (CURRENT ENQUIRY + what the guest just said); leave out what is "
        "unknown. Give check_out OR nights: code works out the other. It asks for anything missing and checks the "
        "rooms; its messages go out for you (see `sent`).",
        {"type": "object", "properties": {
            "place": {"type": "string", "description": "the resort or town as the guest wrote it in this chat"},
            "check_in": DATE_FIELD,
            "check_out": DATE_FIELD,
            "nights": {"type": "integer", "description": "instead of check_out"},
            "adults": {"type": "integer", "description": "adults above 6 years, as the guest said (never assume)"},
            "rooms": {"type": "integer", "description": "only if the guest said a number of rooms"},
            "room_picks": {"type": "array", "description": "rooms the guest CHOSE themselves ('cottage venam', 'room "
                                                         "103', a swipe-reply 'this one' on a room's photo), one entry "
                                                         "per kind; [] clears it. Guest wants a room type → "
                                                         "room_picks=[that type]. Only asking about or wanting to see "
                                                         "another room → send_photos, not room_picks.",
                           "items": {"type": "object", "properties": {
                               "room_type": {"type": "string", "description": "a room type name from FACTS"},
                               "room_number": {"type": "string"},
                               "count": {"type": "integer"}}}},
            "exclude_room_types": {"type": "array", "items": {"type": "string"},
                                   "description": "use ONLY when the guest says they don't want a type ('no cottage')"},
            "extra_beds": {"type": "integer", "description": "only the guest's answer to the extra-bed question (0 = "
                                                             "no) or an extra bed they ask for themselves"},
            "early_checkin": {"type": "boolean", "description": "true only if the guest asked for early check-in"},
            "language": LANGUAGE_FIELD}}),
    ToolSpec(
        "get_price",
        "Every question about price, rate, cost, total or advance (e.g. 'cost of the cottage'). Answers for the stay "
        "already given (or on the open link) and sends the price message. No stay known yet → update_enquiry instead.",
        {"type": "object", "properties": {
            "room_type": {"type": "string", "description": "only if the guest named a room type"},
            "language": LANGUAGE_FIELD}}),
    ToolSpec(
        "send_photos",
        "The guest wants to see rooms or the resort, in any words ('pics?'). Never refuse, never ask for dates first. "
        "Any stage, also after payment. kind: room_type (also pass room_type), resort, other_room, all_rooms, default "
        "(no room named), more. Asking about another room ('is the cottage free?') → other_room or room_type: code says "
        "if it is free and shows it; it never switches their room.",
        {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(pp.KINDS)},
            "room_type": {"type": "string", "description": "only for kind room_type: a room type name from FACTS"},
            "language": LANGUAGE_FIELD},
         "required": ["kind"]}),
    ToolSpec(
        "send_map",
        "The guest asks for the location, map, pin or directions.",
        {"type": "object", "properties": {"language": LANGUAGE_FIELD}}),
    ToolSpec(
        "create_booking",
        "Rooms were found available and the guest gives a mobile number (and a name if they gave one, else empty). "
        "'same number' → mobile 'same'. Never ask 'shall I confirm?'. The summary and payment link go out for you.",
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
        "You do not know the answer or cannot do what the guest asks. NOT for the first discount ask. First discount "
        "ask → answer yourself in one warm line with a real good point from FACTS. Only the SECOND discount ask → "
        "talk_to_owner(other). Its fixed text is sent for you. Reasons: pay_terms = other payment terms (smaller "
        "advance, cash, pay at the property); guest_called = they say they called, visited or paid the owner; "
        "cannot_answer = not in FACTS, or 3+ questions at once; call_request = they want the owner or a call; "
        "change_or_cancel_paid = change or cancel a PAID booking; other = complaint, refund, second discount ask.",
        {"type": "object", "properties": {
            "reason": {"type": "string", "enum": list(OWNER_REASONS)},
            "staff_note": {"type": "string", "description": "optional, for the staff only, e.g. 'asks full refund'"},
            "language": LANGUAGE_FIELD},
         "required": ["reason"]}),
    ToolSpec(
        "repeat_step",
        "A booking step is open (the form, the name and mobile ask, the payment link) and the guest asks for it again "
        "or writes something unclear about it ('what sure'): the step is sent again. NOT for a plain 'ok' / 'thanks' "
        "that only acknowledges.",
        {"type": "object", "properties": {"language": LANGUAGE_FIELD}}),
    ToolSpec(
        "greet",
        "The guest greets, in any words or language ('hi', 'hello', 'hai', 'vanakkam', 'namaste', 'good morning'). Code "
        "sends the right greeting: the owner's welcome for a new conversation, else 'Hi' with their open enquiry and its "
        "step, or their confirmed booking. Write nothing after it unless the guest also asked something.",
        {"type": "object", "properties": {"language": LANGUAGE_FIELD}}),
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


class AgentTools:
    """The tools of one guest turn. `bot` is the BookingBot: its repository, clock, payments and existing helpers."""

    def __init__(self, bot: Any, phone: str, outbox: Outbox, guest_text: str | None = None) -> None:
        self.bot, self.phone, self.out = bot, phone, outbox
        # the guest's message of this turn (run_turn passes it); None = a call outside a guest turn: places not checked
        self.guest_text = guest_text
        self.ran: list[str] = []  # the tools that ran this turn (ONCE_TOOLS run at most once)
        self.quiet = False  # talk_to_owner(cannot_answer / call_request / other / guest_called): no AI line
        self.no_step = False  # talk_to_owner(cannot_answer / call_request / other): no open step after the handoff

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

    def _send(self, key: str, lang: str, **values: Any) -> None:
        """An owner text from i18n.py, in the guest's language."""
        self.out.text(key, t(key, lang, **values))

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

    def _rooms_line(self, prop: Property, parts: list[dict[str, Any]], adults: int, children: int, wanted: int,
                    lang: str) -> tuple[str, str]:
        """The ONE sentence about rooms and extra beds, always worked out in code (the AI never does this maths)."""
        persons, quoted = adults + children, sum(p["rooms"] for p in parts)
        if beds := sum(p["beds"] for p in parts):
            return "rooms_beds", t("rooms_beds", lang, persons=persons, rooms=quoted, beds=beds,
                                   rate=f"{prop.extra_bed_rate:,}")
        fewer = av.fewest_rooms(self.bot.repo.list_room_types(prop.id), adults, children)
        if not wanted and fewer and fewer < quoted:
            return "rooms_note", t("rooms_note", lang, persons=persons, rooms=quoted, fewer=fewer,
                                   rate=f"{prop.extra_bed_rate:,}")
        return "rooms_plain", t("rooms_plain", lang, persons=persons, rooms=quoted)

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
        if place:
            if closed := self._closed_place(place):
                self._send("closed_place", lang,
                           place=(closed.city or closed.place or closed.name).split(",")[0].strip())
                return done(True, {"place": place, "available": False}, SENT_NOTE)
            prop = self._our_place(place)
            if prop is None and self._only_resort() is None:  # a town we have no resort in
                self._send("not_our_place", lang, towns=self._towns_and())
                enq.place = None
                ask = t("ask_location", lang, towns=bot._towns_text())
                if not any(near_same(ask, x) for x in [*self.out.texts, repo.last_outbound_text(phone) or ""]):
                    self.out.text("ask_location", ask)
                else:  # it is the last message already: the turn's open step must not add it either
                    self.out.marker(STEP_SHOWN)
                return done(True, {"place": place, "ours": False}, SENT_NOTE)
            if prop is None:  # one resort only: the guest is told, and the booking goes on with it
                self._send("not_our_place", lang, towns=self._towns_and())
        if prop is None and cur.place:
            prop = repo.get_property(cur.place)
        if prop is None and held:
            prop = repo.get_property(held[0].property_id)
        if prop is None or not prop.active:
            prop = self._only_resort()  # one resort only: the place is set and the location question never goes
        if prop is None or not prop.active:
            enq.place = None
            self._send("ask_location", lang, towns=bot._towns_text())
            facts = {"known": enq.to_dict()["place"]}
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
            self._send("past_date", lang, day=_label(ci))
            enq.check_in = enq.check_out = None
            return done(True, {"problem": "the check-in date has passed"}, SENT_NOTE)
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
            if wish:  # the photos go first, then the dates line and the form (rule 6)
                facts = self._photos(enq, prop, str(wish.get("kind") or "default"), str(wish.get("room_type") or ""), lang)
                if len(self.out.keys) > start:
                    return done(True, {"missing": enq.missing, **facts}, SENT_NOTE)
            got = {"check_in": bool(ci), "check_out": bool(co), "persons": bool(adults), "rooms": wanted}
            self.out.text("form", bot._missing_form(lang, got))
            return done(True, {"missing": enq.missing}, SENT_NOTE)

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
        if wanted and least and wanted < least:  # at most 3 per room: the minimum is said and the check goes on
            self._send("min_rooms", lang, persons=adults + children, rooms=least)
            wanted = bk["rooms"] = least
        enq.rooms = said_rooms or None  # only what the guest asked for is remembered, never the code's own minimum
        picked_before = (cur.checked or {}).get("pref", "")
        if pref and not bot._fits(prop, bk):  # the picked room cannot take them on these dates
            prt = repo.get_room_type(pref)
            self._send("room_pick_no", lang, room=prt.name if prt else "", persons=adults + children)
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
            facts: dict[str, Any] = {"available": False, "check_in": ci.isoformat(), "check_out": co.isoformat()}
            if scratch.get("alt_dates"):
                facts["free_dates"] = scratch["alt_dates"]
            nkey = f"none|{prop.id}|{ci.isoformat()}|{co.isoformat()}|{adults + children}"
            enq.checked = {"available": False, "key": nkey}
            if (cur.checked or {}).get("key") != nkey:
                tag = ("rooms_none" if text == t("rooms_none", lang) else "rooms_none_alt" if scratch.get("alt_dates")
                       else "beds_offer" if scratch.get("suggest_rooms") else "rooms_most")
                self.out.text(tag, text)
                return done(True, facts, SENT_NOTE)
            return done(True, facts, UNCHANGED_NOTE)

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
        facts = {"available": True, "place": bot._place_label(prop), "check_in": ci.isoformat(),
                 "check_out": co.isoformat(), "nights": (co - ci).days, "adults": adults,
                 "rooms": quoted, "rooms_offered": summary, "extra_beds": sum(p["beds"] for p in parts),
                 "extra_bed_question": pending, "early_checkin": early, "price": price}
        if threes:
            facts["extra_beds_max"] = threes
        last = (cur.checked or {}).get("key", "").split("|")
        changed = (cur.checked or {}).get("key") != key
        same_dates = last[:3] == key.split("|")[:3] or (
            bool(held) and key.split("|")[:3] == [held[0].property_id, held[0].check_in.isoformat(),
                                                  held[0].check_out.isoformat()])  # the dates on the open link
        line_key, rooms_line = self._rooms_line(prop, parts, adults, children, wanted, lang)
        # room_picked only when the GUEST chose a room (a room type or number in room_picks); a room the code picked by
        # priority (no pick, or only a refused type) never gets it (fix round 3)
        guest_chose = any(str(p.get("room_type") or "").strip() or str(p.get("room_number") or "").strip()
                          for p in (picks_arg or []))
        picked_now = (guest_chose and bool(pref) and picked_before != pref
                      and not (held and {b.room_type_id for b in held} == {pref}))

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
            return self._relink(enq, prop, bk, parts, mix, held, lang, (line_key, rooms_line), same_dates, picked_now,
                                facts, start)
        if enq.contact and changed and not pending and not held:
            # the answer came after the link was closed: the new summary and link go at once (name and mobile are known)
            return self._relink(enq, prop, bk, parts, mix, [], lang, (line_key, rooms_line), same_dates, picked_now,
                                facts, start, contact=enq.contact)
        if not changed:
            return done(True, facts, UNCHANGED_NOTE)
        if capped:  # more beds than rooms that hold 3 adults: the fixed text names the maximum
            self._send("extra_beds_max", lang, max=capped)
        if answered_only:  # the answer to the extra-bed question: the new rooms sentence if a bed was added, then the ask
            if beds:
                self.out.text(line_key, rooms_line)
            self._send("ask_contact", lang)
            return done(True, facts, SENT_NOTE)
        if picked_now:
            self._send("room_picked", lang, room=offered[0].name)
        shown = list((cur.checked or {}).get("labels") or [])  # the room labels the last room check sent
        if not same_dates:  # new dates: rooms available, the rooms sentence, the photo flow, then the name ask
            if not picked_now:
                self._send("rooms_ok", lang)
            self.out.text(line_key, rooms_line)
            self._stay_photos(enq, prop, parts, bool(mix), price, True, shown)
        else:  # only persons, rooms or the room type changed: the new rooms sentence, a new room's photos, the name ask
            self.out.text(line_key, rooms_line)
            self._stay_photos(enq, prop, parts, bool(mix), price, False, shown)
        if pending:  # a room will hold 3 adults: the extra-bed question, once; the booking waits for the answer
            if ask_now:  # one room holds 3 adults: yes/no; two or more: how many
                self._send("ask_extra_bed" if threes == 1 else "ask_extra_beds", lang)
        else:
            self._send("ask_contact", lang)
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
            self._send(plan.problem, lang, room=plan.room)
            return {"photos": 0, "told": plan.problem}
        if plan.also_free and plan.steps and plan.steps[0][0] is not None:  # another free room: shown, never switched
            self._send("other_room_free", lang, room=plan.steps[0][0].name)
        count = pp.queue_steps(self.out, plan.steps, bot, enq, phone)
        pp.queue_open_step(bot, phone, enq, lang, self.out)  # back to the booking: the step still open
        return {"photos": count, "kind": kind, "rooms": [rt.name for rt, _ in plan.steps if rt is not None]}

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
        if prop is None or not prop.active:  # no resort yet: the location question first, the request is kept (rule 6)
            enq.photo_wish = {"kind": kind, "room_type": room_type}
            self._send("ask_location", lang, towns=bot._towns_text())
            enq.save(repo, phone)
            return self._result(True, {"photos": 0, "waiting_for": "location"}, start, enq,
                                SENT_NOTE + " The photos follow once the guest names the resort.")
        if enq.paid or self._paid_stay(enq):
            # after payment photos are still always allowed: planned without dates, so the guest's own paid booking
            # never makes their room look 'not free' (fix round 4)
            view = Enquiry(place=enq.place, room_picks=enq.room_picks, language=enq.language)
            facts = self._photos(view, prop, kind, room_type, lang)
        else:
            facts = self._photos(enq, prop, kind, room_type, lang)
        enq.save(repo, phone)
        return self._result("problem" not in facts, facts, start, enq,
                            SENT_NOTE if len(self.out.keys) > start else
                            "Nothing was sent. Answer in one short line (no photos are uploaded yet).")

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
                held: list[Any], lang: str, rooms_line: tuple[str, str], same_dates: bool, picked_now: bool,
                facts: dict[str, Any], start: int, contact: dict[str, Any] | None = None) -> dict[str, Any]:
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
        if picked_now:
            self._send("room_picked", lang, room=parts[0]["rt"].name)
        if not same_dates:
            self._send("rooms_ok", lang)
        self.out.text(*rooms_line)
        self.out.text("summary", ctx.summary_message)
        self.out.text("pay", ctx.pay_message)
        enq.booking_id, enq.link_open, enq.contact = made["booking_id"], True, None
        enq.save(bot.repo, self.phone)
        return self._result(True, facts, start, enq, SENT_NOTE + " The old payment link is closed.")

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
            note = ("The guest is told to contact the owner and the team is alerted. Do not promise anything."
                    if ctx.handed_off else "Ask the guest for the missing detail in one short line. Do not guess it.")
            return ctx, made, self._result(False, {"problem": made.get("error", "the payment link could not be made")},
                                           start, enq, note)
        return ctx, made, None

    def _staff(self, reason: str, property_id: str | None, lang: str) -> None:
        """A short summary for the team, through the outbox (a dry run only records it)."""
        text = self.bot.handoff_summary(self.phone, {"lang": lang}, reason)
        self.out.staff("staff:alert", text, self.bot.staff_numbers(property_id))

    def _queue_quiet(self, ctx: _Turn, lang: str, prop: Property | None = None) -> None:
        """What the existing prepare / create code left in a quiet turn, through the outbox: the guest's texts and the
        staff alerts."""
        for message in ctx.after:
            if isinstance(message, str):
                self.out.text("rooms_none" if message == t("rooms_none", lang) else "pay_failed_link", message)
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
        else:
            return self._result(False, {"problem": "no stay is known yet"}, start, enq,
                                "Ask for the missing details with update_enquiry (the owner's form is sent for you).")
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
            if rt is not None:
                self._send("room_not_free", lang, room=rt.name)
            else:
                self._send("rooms_none", lang)
            return self._result(True, {"available": False}, start, enq, SENT_NOTE)
        price = self._price_of(prop, parts, ci, co, early and len(parts) == 1)
        beds = sum(p["beds"] for p in parts)
        room = " + ".join(_count_name(p["rooms"], p["rt"].name) for p in parts) if len(parts) > 1 else parts[0]["rt"].name
        rooms = sum(p["rooms"] for p in parts)
        night = (t("price_night", lang, room=room, rate=_money(price["per_room_per_night"]))
                 if "per_room_per_night" in price else "")
        total = t("price_answer", lang, rooms=rooms, room=room, beds=t("price_beds", lang, beds=beds) if beds else "",
                  dates=av.fmt_range(ci, co), nights=(co - ci).days, total=_money(price["total"]))
        self.out.text("price_answer", "\n".join(x for x in (night, total) if x))
        facts = {"room_type": room, "rooms": rooms, "extra_beds": beds, "nights": (co - ci).days, "price": price}
        return self._result(True, facts, start, enq, "Only a price answer: nothing is booked or changed. " + SENT_NOTE)

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
            self._send("rooms_none", lang)
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
        return self._result(True, {"booked": True, "hold_minutes": bot.config.hold_minutes}, start, enq,
                            SENT_NOTE + " Never say it is booked or confirmed: it is confirmed only when the payment is "
                            "received.")

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
            self._send("pay_wait", lang)
            return self._result(True, {"paid": False, "throttled": True}, start, enq, SENT_NOTE)
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
                                    SENT_NOTE + " The booking is confirmed: say nothing more.")
            if outcome == "refund_needed":  # paid after the hold ran out and the rooms are gone
                self.out.text("owner_will_help", bot._owner_msg("owner_will_help", {"lang": lang, "form_place": b.property_id}))
                return self._result(True, {"paid": True, "rooms_gone": True}, start, enq,
                                    SENT_NOTE + " Never promise a refund; the owner's numbers were given.")
            return self._result(True, {"paid": True, "already_confirmed": True}, start, enq,
                                "The booking was already confirmed: say nothing more.")
        self._send("pay_wait", lang)
        return self._result(True, {"paid": False}, start, enq, SENT_NOTE + " Never say it is confirmed.")

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
        minutes = max(1, int((b.hold_expires_at - now).total_seconds() // 60))
        if reason == "hold":
            self.out.text("hold_pay", t("hold_pay", lang, url=b.payment_link_url))
        else:
            self.out.text("pay", pay_text(b, lang, now))
        enq.booking_id, enq.link_open = b.id, True
        return self._result(True, {"minutes_left": minutes}, start, enq,
                            SENT_NOTE + " Never explain the hold or when the link expires.")

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
            return self._result(False, {"problem": "no resort is known yet"}, start, enq,
                                "Ask which resort with update_enquiry (the location question is sent for you).")
        if not prop.map_url:
            return self._result(True, {"map": False}, start, enq,
                                "No map link is saved for this resort: say so in one line and give the owner's numbers "
                                "with talk_to_owner.")
        self.out.text("map", f"\U0001F4CD {prop.name}\n{prop.map_url}")
        pp.queue_open_step(bot, self.phone, enq, lang, self.out, dates_line=False)  # the map needs no reason for the dates
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
        self._send("name_changed", lang, name=name)
        return self._result(True, {"bookings_changed": changed}, start, enq, SENT_NOTE)

    # ------------------------------------------------------------------ talk_to_owner

    def talk_to_owner(self, args: dict[str, Any]) -> dict[str, Any]:
        """The team gets a short summary; the guest is told to contact the owner directly (with the numbers). The agent
        keeps answering afterwards."""
        bot = self.bot
        start, enq, style, lang = self._ctx(args)
        reason = str(args.get("reason") or "").strip()
        note = str(args.get("staff_note") or "").strip()[:200]
        if reason not in OWNER_REASONS:  # free text instead of a reason: it goes to the staff, the guest gets the handoff
            reason, note = "other", (note or reason)[:200]
        key, staff_line = OWNER_REASONS[reason]
        if reason in QUIET_REASONS:  # only the fixed text goes: the AI's own line is dropped (turn.py)
            self.quiet = True
        if reason in NO_STEP_REASONS:  # ... and no open step / link reminder after it
            self.no_step = True
        if reason == "guest_called":  # the guest already reached the owner: thank them, no staff alert, never deny it
            self._send("guest_called", lang)
            if enq.place or enq.check_in or bot._open_hold(self.phone):  # a booking under way: its open step follows
                pp.queue_open_step(bot, self.phone, enq, lang, self.out, dates_line=False)
            return self._result(True, {"owner_told": False}, start, enq,
                                SENT_NOTE + " Never deny the call, never say there is no record, never promise a call back.")
        held = bot._open_hold(self.phone)
        prop = self._the_resort(enq, held)
        where = {"form_place": prop.id if prop else None}
        if key == "pay_terms":  # other payment terms than the online advance: the owner's text + numbers
            self._send("pay_terms", lang, numbers=bot._owner_numbers(where))
        else:
            self.out.text("handoff", bot._owner_msg("handoff", {"lang": lang, **where}))
        if staff_line is None:  # cannot_answer / call_request: the guest has the numbers, the team is not alerted
            return self._result(True, {"team_told": False, "reason": reason}, start, enq,
                                f"{ALREADY_SENT} Write NOTHING more this turn.")
        if reason == "other" and note:
            staff_line = note  # the staff note says it all
        elif note:
            staff_line = f"{staff_line}: {note}"
        self._staff(staff_line, prop.id if prop else None, lang)
        return self._result(True, {"team_told": True, "reason": reason}, start, enq,
                            (f"{ALREADY_SENT} Write NOTHING more this turn." if reason in QUIET_REASONS else SENT_NOTE)
                            + " Never agree to anything yourself, never promise a call back or say the team will "
                            "contact them.")

    # ------------------------------------------------------------------ repeat_step

    def repeat_step(self, args: dict[str, Any]) -> dict[str, Any]:
        """The open booking step again (live test 2026-11-25: 'ok send me' / 'send' / 'what sure' got nothing or 'Sure'):
        the live payment link, else the location question / form / extra-bed question / name and mobile ask. A repeat is
        allowed here: the guest asked for it."""
        bot = self.bot
        start, enq, style, lang = self._ctx(args)
        now = bot.clock.now()
        if (b := live_link(bot, self.phone)) is not None:
            self.out.text("pay", pay_text(b, lang, now))
            enq.booking_id, enq.link_open = b.id, True
            enq.save(bot.repo, self.phone)
            return self._result(True, {"step": "pay"}, start, enq, SENT_NOTE)
        step = pp.open_step(bot, enq, lang) if enq.active else None  # only while a booking is under way
        if step is None:
            return self._result(False, {"problem": "no booking step is open"}, start, enq,
                                "Nothing is waiting for the guest: answer their message in one short sentence.")
        self.out.text(*step)
        return self._result(True, {"step": step[0]}, start, enq, SENT_NOTE)

    # ------------------------------------------------------------------ greet

    def greet(self, args: dict[str, Any]) -> dict[str, Any]:
        """The guest greeted (live test 2026-11-25: 'hi' got 'Hi.', 'hello' got 'Sure.'). Code picks the owner's text:
        - a NEW conversation (conversation.py: restart, confirmed booking or a 12 h gap): greet ('Hi, How may I assist you?')
        - mid-conversation, a booking under way: hello_back ('Hi. Your enquiry: {stay}.') + the open step, unless that
          step is the last message we sent (the live link's pay text for an open link)
        - mid-conversation, a confirmed booking still ahead: hello_booked
        - mid-conversation, nothing open: 'Hi.'"""
        bot = self.bot
        start, enq, style, lang = self._ctx(args)
        if not conversation.we_spoke(bot, self.phone):
            self.out.text("greet", t("greet", lang))
            return self._result(True, {"greeting": "new_conversation"}, start, enq, SENT_NOTE)
        link = live_link(bot, self.phone)
        booked = self._booked_ahead()
        # an enquiry is open while a booking is under way, unless it is the one that was just paid
        open_enquiry = enq.active and not enq.paid and not (booked is not None and enq.booking_id == booked.id)
        if open_enquiry or link is not None:
            last = pp.last_text(bot, self.phone, self.out)  # read BEFORE the hello is queued
            stay = self._stay_words(enq, link)
            self.out.text("hello_back", t("hello_back", lang, stay=stay) if stay else MID_HI)
            step = ("pay", pay_text(link, lang, bot.clock.now())) if link is not None else pp.open_step(bot, enq, lang)
            if step is not None and step[1] != last:
                self.out.text(*step)
            self.no_step = True  # greet already decided the open step: turn.py's due_step must not add it again
            return self._result(True, {"greeting": "open_enquiry", "stay": stay}, start, enq, SENT_NOTE)
        if booked is not None:
            prop = bot.repo.get_property(booked.property_id)
            self.out.text("hello_booked", t("hello_booked", lang, place=bot._place_label(prop) if prop else "the resort",
                                            dates=f"{_day(booked.check_in)} to {_day(booked.check_out)}"))
            return self._result(True, {"greeting": "confirmed_booking"}, start, enq, SENT_NOTE)
        self.out.text("hello", MID_HI)
        return self._result(True, {"greeting": "mid_conversation"}, start, enq, SENT_NOTE)

    def _stay_words(self, enq: Enquiry, link: Any = None) -> str:
        """'Pozhiyoram, Alleppey, 10 Dec to 12 Dec, 4 person(s), 2 room(s)': what is known of the stay (the open link's
        booking when there is one); '' when nothing is known yet."""
        bot = self.bot
        if link is not None:
            prop = bot.repo.get_property(link.property_id)
            parts = [bot._place_label(prop) if prop else "", f"{_day(link.check_in)} to {_day(link.check_out)}",
                     f"{link.adults} person(s)", f"{link.rooms} room(s)"]
        else:
            prop = bot.repo.get_property(enq.place) if enq.place else None
            dates = (f"{_day(enq.check_in)} to {_day(enq.check_out)}" if enq.check_in and enq.check_out
                     else _day(enq.check_in) if enq.check_in else "")
            parts = [bot._place_label(prop) if prop else "", dates,
                     f"{enq.adults} person(s)" if enq.adults else "",
                     f"{enq.rooms} room(s)" if isinstance(enq.rooms, int) and enq.rooms > 0 else ""]
        return ", ".join(p for p in parts if p)

    def _booked_ahead(self) -> Any:
        """The guest's confirmed booking that is not over yet, or None."""
        today = self.bot._today()
        paid = [b for b in self.bot.repo.list_bookings(phone=self.phone, limit=10)
                if b.status in ("paid", "checked_in") and b.check_out >= today]
        return paid[0] if paid else None

    # ------------------------------------------------------------------ recall_old_chat (read only)

    def recall_old_chat(self, args: dict[str, Any]) -> dict[str, Any]:
        """What was said in this guest's EARLIER conversations (a 12 h gap, a restart or a confirmed booking ends one). Links are hidden."""
        bot = self.bot
        start, enq, style, lang = self._ctx(args)
        rows = bot.repo.list_chat(self.phone, limit=80)
        older = rows[:conversation.start_index(rows)]
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

    # ------------------------------------------------------------------ calling a tool by name

    def call(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        """Run one tool the AI asked for. An unknown name or a crash is a normal result, never an exception."""
        handlers = {"update_enquiry": self.update_enquiry, "get_price": self.get_price, "send_photos": self.send_photos,
                    "send_map": self.send_map, "create_booking": self.create_booking,
                    "check_payment": self.check_payment, "send_payment_link": self.send_payment_link,
                    "my_bookings": self.my_bookings, "change_name": self.change_name,
                    "talk_to_owner": self.talk_to_owner, "recall_old_chat": self.recall_old_chat,
                    "repeat_step": self.repeat_step, "greet": self.greet}
        start, enq = len(self.out.keys), Enquiry.load(self.bot.repo, self.phone)
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
