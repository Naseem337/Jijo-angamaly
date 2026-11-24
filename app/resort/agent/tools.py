"""The agent's tools (docs/AI_AGENT_DESIGN.md section 3). Step AI-2a: update_enquiry and get_price.

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

from datetime import date, timedelta
from typing import Any

from app.providers.llm import ToolSpec
from app.resort import availability as av
from app.resort.agent import photos_plan as pp
from app.resort.agent.outbox import Outbox
from app.resort.agent.state import LANGUAGES, Enquiry
from app.resort.chat import _Turn, _count_name, _money, close_word
from app.resort.i18n import t
from app.resort.models import Property, RoomType

I18N_LANG = {"en": "en", "manglish": "mn", "tanglish": "tn", "hinglish": "hn"}

SENT_NOTE = ("The owner's messages listed in `sent` were already sent to the guest: never write them again. Add ONE short "
             "line of your own only if the guest also asked something else.")
UNCHANGED_NOTE = ("Nothing new to send: the stay is unchanged. Answer only what the guest asked, in one short line.")

LANGUAGE_FIELD = {"type": "string", "enum": list(LANGUAGES),
                  "description": "the language style of the guest's latest message (manglish = Malayalam in English letters)"}

TOOLS = [
    ToolSpec(
        "update_enquiry",
        "Call this whenever the guest says anything about their stay: the place, dates or nights, persons, rooms, a room "
        "type or number, 'one more room', 'make it 5 persons', 'change to jan 2-5', 'no cottage'. Pass the FULL stay as "
        "you now understand it (CURRENT ENQUIRY plus what the guest just said), not only the change. Leave out what is "
        "still unknown. The tool asks the guest for anything missing, checks the rooms, and sends the owner's messages "
        "(listed in `sent`); never write those yourself.",
        {"type": "object", "properties": {
            "place": {"type": "string", "description": "the resort or town the guest named, spelled however they wrote it"},
            "check_in": {"type": "string", "description": "YYYY-MM-DD; a date with no year is the next time it comes"},
            "check_out": {"type": "string", "description": "YYYY-MM-DD"},
            "nights": {"type": "integer", "description": "instead of check_out, if the guest gave nights"},
            "adults": {"type": "integer", "description": "total persons as the guest said them (never assume)"},
            "children": {"type": "integer"},
            "rooms": {"type": "integer", "description": "only if the guest said a number of rooms; 0 = let the code pick"},
            "room_picks": {"type": "array", "description": "rooms the guest chose, one entry per kind; [] clears the choice",
                           "items": {"type": "object", "properties": {
                               "room_type": {"type": "string", "description": "a room type name from the FACTS"},
                               "room_number": {"type": "string"},
                               "count": {"type": "integer"}}}},
            "exclude_room_types": {"type": "array", "items": {"type": "string"},
                                   "description": "room types the guest does not want"},
            "extra_beds": {"type": "integer"},
            "language": LANGUAGE_FIELD}}),
    ToolSpec(
        "send_photos",
        "Send the owner's photos when the guest asks to see the resort or its rooms. Say only WHAT they asked for with "
        "`kind`; the owner's rules decide which photos go (never one twice, a label before each room's photos, only free "
        "rooms once the dates are known). kind: room_type (a named room: also pass room_type), resort (outside, beach, "
        "common areas), other_room (another room than the one offered), all_rooms (every room), default (room photos "
        "with no room named), more (the guest wants more photos). No resort known yet: the location question goes first "
        "and the photos follow the answer.",
        {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(pp.KINDS)},
            "room_type": {"type": "string", "description": "only for kind room_type: the room type name from the FACTS"},
            "language": LANGUAGE_FIELD},
         "required": ["kind"]}),
    ToolSpec(
        "get_price",
        "The price of the stay the guest already gave (or of the stay on their open payment link), worked out now from the "
        "dashboard rates. Call it for EVERY price / rate / cost / how-much question, also for one named room type. It only "
        "answers: nothing is booked or changed and the room is never switched. The price message is sent for you.",
        {"type": "object", "properties": {
            "room_type": {"type": "string", "description": "only if the guest named a room type"},
            "language": LANGUAGE_FIELD}}),
]


def _int(args: dict[str, Any], key: str, default: int | None = None) -> int | None:
    """An integer argument; missing or null = `default` (the saved value)."""
    value = args.get(key)
    if value is None or value == "":
        return default
    return int(value)


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
            beds_left = max(0, bk["adults"] + bk["children"] - sum(av.base_per_room(rt) * n for rt, n in mix)) \
                if prop.extra_bed_rate else 0
            parts = []
            for rt, n in mix:
                beds = min(beds_left, n * max(0, rt.max_occupancy - av.base_per_room(rt)))
                beds_left -= beds
                parts.append({"rt": rt, "rooms": n, "beds": beds})
            return parts, mix
        fits = bot._fits(prop, bk)
        if not fits:
            return [], None
        rt, ok = fits[0]
        rooms = wanted if wanted in ok else ok[0]
        return [{"rt": rt, "rooms": rooms, "beds": bot._beds_for(prop, rt, bk["adults"], bk["children"], rooms)}], None

    def _price_of(self, prop: Property, parts: list[dict[str, Any]], ci: date, co: date) -> dict[str, Any]:
        """Totals from the dashboard rates now. These are the only amounts the AI may use."""
        total = advance = 0
        rates: set[int] = set()
        for part in parts:
            q = av.quote(prop, part["rt"], self.bot.repo.list_rate_rules(part["rt"].id), ci, co, part["rooms"], part["beds"])
            total += q.total
            advance += q.advance
            rates |= {rate for _, rate in q.per_night}
            part["total"], part["advance"] = q.total, q.advance
        price: dict[str, Any] = {"total": total, "advance_now": advance, "balance_at_property": total - advance}
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
        fewer = av.fewest_rooms(self.bot.repo.list_room_types(prop.id), adults, children) if prop.extra_bed_rate else 0
        if not wanted and prop.extra_bed_rate and fewer and fewer < quoted:
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
        children = _int(args, "children", cur.children or 0) or 0
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

        enq.check_in, enq.check_out, enq.adults, enq.children = ci, co, adults, children
        enq.extra_beds = beds_arg
        enq.rooms = wanted or None

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
                              "adults": adults, "children": children, "rooms": wanted, "own": phone}
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
        key = f"{prop.id}|{ci.isoformat()}|{co.isoformat()}|{adults + children}|{sum(p['rooms'] for p in parts)}|{pref}"
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
        price = self._price_of(prop, parts, ci, co)
        offered = [p["rt"] for p in parts]
        summary = " + ".join(_count_name(p["rooms"], p["rt"].name) for p in parts)
        enq.checked = {"available": True, "summary": summary, "rooms": quoted, "key": key, "pref": pref}
        facts = {"available": True, "place": bot._place_label(prop), "check_in": ci.isoformat(),
                 "check_out": co.isoformat(), "nights": (co - ci).days, "adults": adults, "children": children,
                 "rooms": quoted, "rooms_offered": summary, "extra_beds": sum(p["beds"] for p in parts),
                 "price": price}
        last = (cur.checked or {}).get("key", "").split("|")
        changed = (cur.checked or {}).get("key") != key
        same_dates = last[:3] == key.split("|")[:3] or (
            bool(held) and key.split("|")[:3] == [held[0].property_id, held[0].check_in.isoformat(),
                                                  held[0].check_out.isoformat()])  # the dates on the open link
        line_key, rooms_line = self._rooms_line(prop, parts, adults, children, wanted, lang)
        picked_now = bool(pref) and picked_before != pref

        if held and changed:  # a change after the payment link: the old link is closed, a new summary and link go
            held_key = f"{prop.id}|{ci.isoformat()}|{co.isoformat()}|{adults + children}|{sum(b.rooms for b in held)}"
            held_types = {b.room_type_id for b in held}
            if key.rsplit("|", 1)[0] == held_key and held_types == {rt.id for rt in offered}:
                return done(True, facts, UNCHANGED_NOTE)  # the same stay as the open link
            return self._relink(enq, prop, bk, parts, mix, held, lang, (line_key, rooms_line), same_dates, picked_now,
                                facts, start)
        if not changed:
            return done(True, facts, UNCHANGED_NOTE)
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
        self._send("ask_contact", lang)
        return done(True, facts, SENT_NOTE)

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
                facts: dict[str, Any], start: int) -> dict[str, Any]:
        """The stay changed after the payment link went out: the existing prepare / create code makes the new booking and
        link and closes the old one (close_old_holds); the name and mobile come from the held booking."""
        bot = self.bot
        stay = bot._held_stay(self.phone) or {}
        if mix:
            bk["mix"] = [{"room_type_id": p["rt"].id, "rooms": p["rooms"], "beds": p["beds"], "total": p["total"],
                          "advance": p["advance"]} for p in parts]
        scratch = {"lang": lang, "style": enq.language, "turn": 1, "bk": bk}
        # quiet: nothing is read from the old chat log and nothing is sent past the outbox
        ctx = _Turn(self.phone, scratch, text="", quiet=True)
        guest = bot.repo.get_guest(self.phone)
        name = stay.get("guest_name") or (guest.name if guest else "")
        prep = bot._tool_prepare(ctx, {"rooms": sum(p["rooms"] for p in parts)})
        if "error" in prep:
            self._queue_quiet(ctx, lang)  # e.g. the last rooms were just taken: the owner's text
            enq.save(bot.repo, self.phone)
            return self._result(False, {"problem": prep["error"]}, start, enq, "Tell the guest nothing invented.")
        if not name:
            enq.save(bot.repo, self.phone)
            return self._result(False, {"problem": "the guest's name is not known"}, start, enq,
                                "Ask the guest for their name, then call create_booking.")
        made = bot._tool_create(ctx, {"guest_name": name, "mobile": stay.get("mobile") or "same"})
        if "error" in made or ctx.handed_off:
            self._queue_quiet(ctx, lang)  # the sold-out text, or the owner's numbers when the link could not be made
            enq.save(bot.repo, self.phone)
            return self._result(False, {"problem": made.get("error", "the payment link could not be made")}, start, enq,
                                "The guest is told to contact the owner. Do not promise anything.")
        if picked_now:
            self._send("room_picked", lang, room=parts[0]["rt"].name)
        if not same_dates:
            self._send("rooms_ok", lang)
        self.out.text(*rooms_line)
        self.out.text("summary", ctx.summary_message)
        self.out.text("pay", ctx.pay_message)
        enq.booking_id, enq.link_open = made["booking_id"], True
        enq.save(bot.repo, self.phone)
        return self._result(True, facts, start, enq, SENT_NOTE + " The old payment link is closed.")

    def _queue_quiet(self, ctx: _Turn, lang: str) -> None:
        """Texts the existing prepare / create code left for the guest in a quiet turn, through the outbox."""
        for message in ctx.after:
            if isinstance(message, str):
                self.out.text("rooms_none" if message == t("rooms_none", lang) else "pay_failed_link", message)

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
            ci, co, adults, children = enq.check_in, enq.check_out, enq.adults, enq.children or 0
            wanted, pref = enq.rooms or 0, ""
            if len({p.get("room_type") for p in enq.room_picks}) == 1:
                rt0 = self._room(repo.list_room_types(prop.id), enq.room_picks[0]["room_type"]) if prop else None
                pref = rt0.id if rt0 else ""
        elif held:  # the payment link is out: its stay is what we know
            prop = repo.get_property(held["property_id"])
            ci, co = date.fromisoformat(held["check_in"]), date.fromisoformat(held["check_out"])
            adults, children, wanted = held["adults"], held["children"], int(held.get("rooms") or 0)
            pref = held["room_type_ids"][0] if len(set(held["room_type_ids"])) == 1 else ""
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
        if not parts:
            rt = repo.get_room_type(pref) if pref else None
            if rt is not None:
                self._send("room_not_free", lang, room=rt.name)
            else:
                self._send("rooms_none", lang)
            return self._result(True, {"available": False}, start, enq, SENT_NOTE)
        price = self._price_of(prop, parts, ci, co)
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
