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

import logging
import time
from datetime import date, timedelta
from typing import Any

from app.providers.llm import ToolSpec
from app.resort import availability as av
from app.resort.agent import photos_plan as pp
from app.resort.agent.outbox import Outbox
from app.resort.agent.state import LANGUAGES, Enquiry
from app.resort.chat import _Turn, _count_name, _money, close_word
from app.resort.i18n import status_name, t
from app.resort.models import Property, RoomType
from app.resort.refunds import cancellation_terms

log = logging.getLogger(__name__)

I18N_LANG = {"en": "en", "manglish": "mn", "tanglish": "tn", "hinglish": "hn"}

SENT_NOTE = ("The owner's messages listed in `sent` were already sent to the guest: never write them again. Add ONE short "
             "line of your own only if the guest also asked something else.")
UNCHANGED_NOTE = ("Nothing new to send: the stay is unchanged. Answer only what the guest asked, in one short line.")

LANGUAGE_FIELD = {"type": "string", "enum": list(LANGUAGES),
                  "description": "the language style of the guest's latest message (manglish = Malayalam in English letters)"}

TOOL_SCHEMAS = [
    ToolSpec(
        "update_enquiry",
        "Use this whenever the guest says anything about their stay: the place, dates or nights, number of adults, number "
        "of rooms, a room type or room number, 'one more room', 'make it 5 persons', 'change to jan 2-5', 'no cottage'. "
        "Pass the FULL stay as you now understand it (CURRENT ENQUIRY plus what the guest just said), not only the change, "
        "and leave out what is still unknown. It asks the guest for anything missing and checks the rooms, and the owner's "
        "messages go out for you (see `sent`): never write those yourself.",
        {"type": "object", "properties": {
            "place": {"type": "string", "description": "the resort or town the guest named, spelled however they wrote it"},
            "check_in": {"type": "string", "description": "YYYY-MM-DD; a date with no year is the next time it comes"},
            "check_out": {"type": "string", "description": "YYYY-MM-DD"},
            "nights": {"type": "integer", "description": "instead of check_out, if the guest gave nights"},
            "adults": {"type": "integer", "description": "total adults above 6 years as the guest said them (never assume); "
                                                         "children are not asked or counted"},
            "rooms": {"type": "integer", "description": "only if the guest said a number of rooms; 0 = let the code pick"},
            "room_picks": {"type": "array", "description": "rooms the guest chose, one entry per kind; [] clears the choice",
                           "items": {"type": "object", "properties": {
                               "room_type": {"type": "string", "description": "a room type name from the FACTS"},
                               "room_number": {"type": "string"},
                               "count": {"type": "integer"}}}},
            "exclude_room_types": {"type": "array", "items": {"type": "string"},
                                   "description": "room types the guest does not want"},
            "extra_beds": {"type": "integer", "description": "pass it ONLY when the guest answers the extra-bed question "
                                                                "('Do you need an extra bed?' or 'How many extra beds do you "
                                                                "need?': the number, 0 for no) or asks for an extra bed on "
                                                                "their own; the code caps it"},
            "early_checkin": {"type": "boolean", "description": "true only if the guest asked for early check-in (it adds "
                                                                "a charge); false removes it"},
            "language": LANGUAGE_FIELD}}),
    ToolSpec(
        "get_price",
        "Use this for EVERY question about price, rate, cost, total or advance, also for one named room type ('cost of the "
        "cottage'). It answers for the stay the guest already gave (or the stay on their open payment link) and sends the "
        "price message for you. It never books or switches anything. If no stay is known yet, call update_enquiry instead.",
        {"type": "object", "properties": {
            "room_type": {"type": "string", "description": "only if the guest named a room type"},
            "language": LANGUAGE_FIELD}}),
    ToolSpec(
        "send_photos",
        "Use this when the guest asks to see photos or pictures of the resort or its rooms. Say only WHAT they asked for "
        "with `kind`: room_type (a named room: also pass room_type), resort (outside, beach, common areas), other_room "
        "(another room than the one offered), all_rooms (every room), default (room photos, no room named), more (they want "
        "more photos). If no resort is known yet the location question goes first and the photos follow the answer.",
        {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(pp.KINDS)},
            "room_type": {"type": "string", "description": "only for kind room_type: the room type name from the FACTS"},
            "language": LANGUAGE_FIELD},
         "required": ["kind"]}),
    ToolSpec(
        "send_map",
        "Use this when the guest asks where the resort is, or for the location, the map, the pin or directions.",
        {"type": "object", "properties": {"language": LANGUAGE_FIELD}}),
    ToolSpec(
        "create_booking",
        "Use this as soon as rooms were found available and the guest gives their mobile number (and their name, if they "
        "gave one). 'this number', 'same number' or 'same' means the WhatsApp number: pass mobile 'same'. The name is not "
        "required: if they gave none, pass an empty name. Do not ask 'shall I confirm?'. The enquiry summary and the "
        "payment link are sent for you.",
        {"type": "object", "properties": {
            "guest_name": {"type": "string", "description": "the name the guest gave, or empty"},
            "mobile": {"type": "string", "description": "the mobile number the guest gave, or 'same' for this WhatsApp number"},
            "language": LANGUAGE_FIELD},
         "required": ["mobile"]}),
    ToolSpec(
        "check_payment",
        "Use this when the guest says they have paid or are paying now, sends a payment screenshot, or asks whether their "
        "payment arrived. Only this tool can confirm a payment: never confirm it yourself from their word or a screenshot. "
        "The right message is sent for you.",
        {"type": "object", "properties": {"language": LANGUAGE_FIELD}}),
    ToolSpec(
        "send_payment_link",
        "Use this when the guest asks for the payment link again or cannot open it (reason lost), wants to hold or reserve "
        "the room or pay later (reason hold), or says they have not paid yet (reason not_paid_yet). It only works while the "
        "room is held; if the hold ran out it tells you and sends nothing, and you offer to check the dates again.",
        {"type": "object", "properties": {
            "reason": {"type": "string", "enum": ["lost", "hold", "not_paid_yet"]},
            "language": LANGUAGE_FIELD},
         "required": ["reason"]}),
    ToolSpec(
        "my_bookings",
        "Use this when the guest asks about a booking or payment they already have (dates, status, what was paid, the "
        "balance), or wants to cancel: it gives the cancellation charge if they cancelled today. Read only.",
        {"type": "object", "properties": {}}),
    ToolSpec(
        "change_name",
        "Use this when the guest corrects the name on their enquiry or booking ('my name is not X, it is Y'). Never start "
        "the booking again for a name correction.",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "the correct name, in English letters"},
            "language": LANGUAGE_FIELD},
         "required": ["name"]}),
    ToolSpec(
        "talk_to_owner",
        "Use this when you do not know the answer or cannot do what the guest asks: they ask for the owner or a call, "
        "complain, want a refund, a discount or an exception, or want to change a booking that is already paid. The owner "
        "gets a short summary and the guest is told to contact the owner directly. Never promise a call back.",
        {"type": "object", "properties": {
            "reason": {"type": "string", "description": "one short line for the owner, e.g. 'wants to cancel, asks full refund'"},
            "language": LANGUAGE_FIELD},
         "required": ["reason"]}),
    ToolSpec(
        "recall_old_chat",
        "Use this only when the guest refers to something said or sent in an EARLIER conversation ('as I told you', 'the "
        "link you sent', 'my last enquiry'). Never for a greeting, small talk or a fresh request. Read only.",
        {"type": "object", "properties": {"query": {"type": "string", "description": "what you are looking for"}}}),
]
TOOLS = TOOL_SCHEMAS  # the same list, under the name AI-2a used

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


def _day(value: Any) -> date | None:
    return date.fromisoformat(str(value)) if value else None


def _label(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


class AgentTools:
    """The tools of one guest turn. `bot` is the BookingBot: its repository, clock, payments and existing helpers."""

    def __init__(self, bot: Any, phone: str, outbox: Outbox) -> None:
        self.bot, self.phone, self.out = bot, phone, outbox

    # ------------------------------------------------------------------ helpers

    def _result(self, ok: bool, facts: dict[str, Any], start: int, enq: Enquiry, note: str) -> dict[str, Any]:
        return {"ok": ok, "facts": facts, "sent": self.out.keys[start:], "next_step": enq.next_step, "note": note}

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
        style = args.get("language") if args.get("language") in LANGUAGES else cur.language
        lang = I18N_LANG[style]
        enq = Enquiry.from_dict(cur.to_dict())
        enq.language = style
        held = bot._open_hold(phone)
        enq.booking_id, enq.link_open = (held[0].id, True) if held else (None, False)

        def done(ok: bool, facts: dict[str, Any], note: str) -> dict[str, Any]:
            enq.save(repo, phone)
            return self._result(ok, facts, start, enq, note)

        # 1. the place: unknown -> the owner's location question; switched off -> closed_place
        place = str(args.get("place") or "").strip()
        prop: Property | None = None
        if place:
            if closed := self._closed_place(place):
                self._send("closed_place", lang,
                           place=(closed.city or closed.place or closed.name).split(",")[0].strip())
                return done(True, {"place": place, "available": False}, SENT_NOTE)
            prop = bot._find_property(place)
        if prop is None and cur.place:
            prop = repo.get_property(cur.place)
        if prop is None and held:
            prop = repo.get_property(held[0].property_id)
        if prop is None or not prop.active:
            enq.place = None
            self._send("ask_location", lang, towns=bot._towns_text())
            return done(True, {"known": enq.to_dict()["place"]}, SENT_NOTE)
        enq.place = prop.id
        wish, enq.photo_wish = cur.photo_wish, None  # photos asked for before the resort was known

        # the stay as the AI passes it; what it leaves out stays as saved (a field the AI forgets is never lost)
        try:
            ci, co = _day(args.get("check_in")), _day(args.get("check_out"))
        except ValueError:
            return done(False, {"problem": "check_in and check_out must be YYYY-MM-DD"}, "Fix the dates and call again.")
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

        # 3. a date in the past: a year-less date goes to the next time it comes, a date that just passed is told
        if ci and (today - ci).days > 14:
            for years in (1, 2):
                try:
                    moved = ci.replace(year=ci.year + years)
                except ValueError:
                    continue
                if moved >= today:
                    co = co + (moved - ci) if co else co
                    ci = moved
                    break
        if ci and ci < today:
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
                       "base": base, "extra_bed_pending": pending, "extra_bed_max": threes}
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
        picked_now = bool(pref) and picked_before != pref

        held_same = False  # is this the stay on the open link?
        if held:
            held_key = (f"{prop.id}|{ci.isoformat()}|{co.isoformat()}|{sum(b.adults for b in held)}|"
                        f"{sum(b.rooms for b in held)}")  # the stay ON THE LINK, not the one the guest now asks for
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
        if not same_dates:  # new dates: rooms available, the rooms sentence, the photo flow, then the name ask
            if not picked_now:
                self._send("rooms_ok", lang)
            self.out.text(line_key, rooms_line)
            self._stay_photos(enq, prop, parts, bool(mix), price, True)
        else:  # only persons, rooms or the room type changed: the new rooms sentence, a new room's photos, the name ask
            self.out.text(line_key, rooms_line)
            self._stay_photos(enq, prop, parts, bool(mix), price, False)
        if pending:  # a room will hold 3 adults: the extra-bed question, once; the booking waits for the answer
            if ask_now:  # one room holds 3 adults: yes/no; two or more: how many
                self._send("ask_extra_bed" if threes == 1 else "ask_extra_beds", lang)
        else:
            self._send("ask_contact", lang)
        return done(True, facts, SENT_NOTE + (" The old payment link is closed: a new summary and link follow the guest's "
                                              "answer to the extra-bed question." if closed_link else ""))

    def _stay_photos(self, enq: Enquiry, prop: Property, parts: list[dict[str, Any]], mixed: bool,
                     price: dict[str, Any], first_time: bool) -> None:
        """The owner's order after a room check (rule 2): the glimpse, the general photos not sent yet, then each room's
        label and photos. Only the room photos when just the persons or rooms changed on the same dates."""
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
        for part in (parts if mixed else parts[:1]):  # both room types only when the group is split over two
            if refs := [x for x in pp.room_refs(self.bot, part["rt"], enq, part["rooms"]) if x not in sent]:
                pp.queue_room(self.out, part["rt"], refs[:pp.MAX_ROOM_PHOTOS])

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
        if kind == "other_room" and enq.checked and plan.steps and plan.steps[0][0] is not None:
            self._send("other_room_free", lang, room=plan.steps[0][0].name)
        count = pp.queue_steps(self.out, plan.steps)
        pp.queue_open_step(bot, phone, enq, lang, self.out)  # back to the booking: the step still open
        return {"photos": count, "kind": kind, "rooms": [rt.name for rt, _ in plan.steps if rt is not None]}

    def send_photos(self, args: dict[str, Any]) -> dict[str, Any]:
        bot, phone, repo = self.bot, self.phone, self.bot.repo
        start = len(self.out.keys)
        enq = Enquiry.load(repo, phone)
        style = args.get("language") if args.get("language") in LANGUAGES else enq.language
        lang = I18N_LANG[style]
        enq.language = style
        kind, room_type = str(args.get("kind") or "default"), str(args.get("room_type") or "").strip()
        if kind not in pp.KINDS:
            return self._result(False, {"problem": f"kind must be one of {', '.join(pp.KINDS)}"}, start, enq,
                                "Call again with a valid kind.")
        held = bot._open_hold(phone)
        if held:  # the payment link is out: that is the booking step (nothing is asked again after the photos)
            enq.booking_id, enq.link_open = held[0].id, True
        prop = repo.get_property(enq.place) if enq.place else None
        if prop is None and held:
            prop = repo.get_property(held[0].property_id)
        if prop is None or not prop.active:  # no resort yet: the location question first, the request is kept (rule 6)
            enq.photo_wish = {"kind": kind, "room_type": room_type}
            self._send("ask_location", lang, towns=bot._towns_text())
            enq.save(repo, phone)
            return self._result(True, {"photos": 0, "waiting_for": "location"}, start, enq,
                                SENT_NOTE + " The photos follow once the guest names the resort.")
        facts = self._photos(enq, prop, kind, room_type, lang)
        enq.save(repo, phone)
        return self._result("problem" not in facts, facts, start, enq,
                            SENT_NOTE if len(self.out.keys) > start else
                            "Nothing was sent. Answer in one short line (no photos are uploaded yet).")

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
        style = args.get("language") if args.get("language") in LANGUAGES else enq.language
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
        style = args.get("language") if args.get("language") in LANGUAGES else enq.language
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
                return self._result(False, {"hold_expired": True}, start, enq,
                                    "The hold ran out and nothing was sent. Offer to check the dates again with update_enquiry.")
            return self._result(False, {"open_link": False}, start, enq,
                                "No payment link is open for this guest: follow next_step.")
        b = live[0]
        minutes = max(1, int((b.hold_expires_at - now).total_seconds() // 60))
        if reason == "hold":
            self.out.text("hold_pay", t("hold_pay", lang, url=b.payment_link_url))
        else:
            self.out.text("pay", t("pay", lang, id=b.id, minutes=minutes, advance=f"{b.advance:,}", url=b.payment_link_url))
        enq.booking_id, enq.link_open = b.id, True
        return self._result(True, {"minutes_left": minutes}, start, enq,
                            SENT_NOTE + " Never explain the hold or when the link expires.")

    # ------------------------------------------------------------------ send_map

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
        reason = (str(args.get("reason") or "").strip() or "the guest asked for the owner")[:200]
        held = bot._open_hold(self.phone)
        prop = self._the_resort(enq, held)
        self.out.text("handoff", bot._owner_msg("handoff", {"lang": lang, "form_place": prop.id if prop else None}))
        self._staff(reason, prop.id if prop else None, lang)
        return self._result(True, {"owner_told": True}, start, enq,
                            SENT_NOTE + " Never promise a call back or say the owner will contact them.")

    # ------------------------------------------------------------------ recall_old_chat (read only)

    def recall_old_chat(self, args: dict[str, Any]) -> dict[str, Any]:
        """What was said in this guest's EARLIER conversations (a 24 h gap or a restart ends one). Links are hidden."""
        bot = self.bot
        start, enq, style, lang = self._ctx(args)
        rows = bot.repo.list_chat(self.phone, limit=80)
        older = rows[:bot._session_start(rows, by_time=True)]
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
                    "talk_to_owner": self.talk_to_owner, "recall_old_chat": self.recall_old_chat}
        start, enq = len(self.out.keys), Enquiry.load(self.bot.repo, self.phone)
        handler = handlers.get(name)
        if handler is None:
            return self._result(False, {"problem": f"unknown tool {name}"}, start, enq, "Use one of the listed tools.")
        try:
            return handler(args or {})
        except Exception as exc:  # a bad tool call must never leave the guest unanswered
            log.exception("Tool %s failed", name)
            return self._result(False, {"problem": f"internal error: {type(exc).__name__}"}, start, enq,
                                "Say nothing invented; use talk_to_owner.")
