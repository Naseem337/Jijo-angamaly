"""The owner's photo rules (handoff section 5b, rules 1-8), moved out of chat.py with the same behaviour.

The old bot found out WHAT the guest wanted with regexes (PHOTO_ASK, ROOM_OPTIONS_ASK, OTHER_ROOM, ALL_ROOMS ...). Here the
AI decides and passes `kind`; this file only applies the owner's rules to it:

  1. a photo is never sent twice in a conversation;
  2. the order after a room check: rooms_ok -> rooms sentence -> glimpse -> general photos -> FOR EACH offered room its
     label '*Room <number> - <room type>*' (even with no photo) + its photos -> ask_contact;
  3. a room's photos are those of the exact room the guest would get (108 and 109 can look different);
  4. kinds: room_type (a named room, only if free on the guest's dates), resort (general photos only), other_room (the
     next free room by priority), all_rooms (every free room not shown yet), default / more (the offered room, else the
     next free one, else the resort's own photos once every room was shown);
  5. before the dates room photos go freely, then the dates line and the form once;
  6. no resort yet: the location question first, the request is kept and answered once the place is known;
  7. a room label (text) goes before each room's photos;
  8. after the photos the booking step still open is repeated, unless it is the message just above.

No text matching of any kind happens here (no `re`, no keyword lists): room names the AI passes are matched by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.resort import availability as av
from app.resort.agent.outbox import Outbox
from app.resort.agent.state import Enquiry
from app.resort.i18n import t
from app.resort.models import Booking, Property, RoomType

KINDS = ("room_type", "resort", "other_room", "all_rooms", "default", "more")
MAX_ROOM_PHOTOS = None  # no limit (owner, 2026-11-19): every photo uploaded in the dashboard goes
MAX_GENERAL_PHOTOS = None


@dataclass
class Plan:
    steps: list[tuple[RoomType | None, list[str]]] = field(default_factory=list)  # (room or None for general, photos)
    problem: str = ""  # an i18n key: photos_sent | no_other_room | room_not_free | other_free
    room: str = ""  # the room's name for that text
    also_free: bool = False  # the room shown is ANOTHER free room than the one offered (dates known): 'Yes, X is also
    # free for your dates' goes first; it only shows the room, the offer is never switched


# ------------------------------------------------------------------ what was sent


def queued_photos(out: Outbox) -> set[str]:
    """Photos already in this turn's outbox (sent or waiting): a photo is never twice in one turn either."""
    return {ref for i in (*out.sent, *out.queue) if i.kind == "photos" for ref in i.refs}


def sent_refs(bot: Any, phone: str, out: Outbox) -> set[str]:
    """Every photo already sent in this conversation (the '[photo] <ref>' rows of the chat log) or queued now."""
    return set(bot._sent_photos(phone)) | queued_photos(out)


def last_text(bot: Any, phone: str, out: Outbox) -> str:
    """The newest text we sent or queued for this guest."""
    for item in reversed([*out.sent, *out.queue]):
        if item.kind == "text":
            return item.text
    return bot.repo.last_outbound_text(phone) or ""


def already_said(bot: Any, phone: str, out: Outbox, text: str) -> bool:
    return any(i.kind == "text" and i.text == text for i in (*out.sent, *out.queue)) or bot._already_sent(phone, text)


# ------------------------------------------------------------------ rooms and their photos


def find_room(bot: Any, rooms: list[RoomType], name: str) -> RoomType | None:
    """A room type by the name the AI passed: any case, a part of the name, or a close spelling."""
    said = (name or "").strip().casefold()
    for rt in rooms:
        if rt.name.casefold() == said or rt.id.casefold() == said:
            return rt
    for rt in rooms:
        if said and (said in rt.name.casefold() or rt.name.casefold() in said):
            return rt
    return bot._room_named(name, rooms) if said else None


def next_numbers(bot: Any, rt: RoomType, enq: Enquiry, rooms: int = 1, phone: str = "") -> list[str]:
    """The room numbers the guest would get: the first free ones by the owner's order on their dates (the same rule as
    when they pay). The guest's OWN unpaid hold never counts as taken (a new summary replaces it). Without dates: the
    first numbers in the list."""
    if not rt.room_list:
        return []
    if not (enq.check_in and enq.check_out):
        return rt.room_list[:rooms]
    now = bot.clock.now()
    probe = Booking(rt.property_id, rt.id, "", enq.check_in, enq.check_out, 1, 0, rooms, 0, 0, now)
    taken = [b for b in bot.repo.blocking_bookings(rt.id, enq.check_in, enq.check_out)
             if not (phone and b.status == "hold" and b.guest_phone == phone)]
    return av.allocate_rooms(rt, probe, taken, now) or rt.room_list[:rooms]


def room_label(rt: RoomType, number: str = "") -> str:
    """'*Room 108 - Beach View Standard*': the room the guest would get and its room type name exactly as in the
    dashboard ('*Beach View Standard*' when the room type has no room numbers)."""
    return f"*Room {number} - {rt.name}*" if number else f"*{rt.name}*"


def offered_sets(bot: Any, rt: RoomType, enq: Enquiry, rooms: int, sent: set[str], phone: str = ""
                 ) -> list[tuple[str, list[str]]]:
    """Each offered room of this type as (label, its photos not sent yet), one per room the guest would get. A room
    with no photos of its own shows the room type's photos (once). A label goes even when there is no photo."""
    used = set(sent)
    out: list[tuple[str, list[str]]] = []
    for number in next_numbers(bot, rt, enq, rooms, phone) or [""]:
        own = rt.number_photos.get(number, []) if number else []
        refs = [r for r in dict.fromkeys(own or rt.photo_urls) if r not in used]
        used |= set(refs)
        out.append((room_label(rt, number), refs))
    return out


def room_refs(bot: Any, rt: RoomType, enq: Enquiry, rooms: int = 1, phone: str = "") -> list[str]:
    """Photos of the exact room the guest would get, else the room type's photos, else any room number's photos."""
    own = [r for n in next_numbers(bot, rt, enq, rooms, phone) for r in rt.number_photos.get(n, [])]
    if own:
        return list(dict.fromkeys(own))
    if rt.photo_urls:
        return list(rt.photo_urls)
    return list(dict.fromkeys(r for n in rt.room_list for r in rt.number_photos.get(n, [])))


def label_sets(rt: RoomType, refs: list[str]) -> list[tuple[str, list[str]]]:
    """A room type's photos split by room number, each with its label ('*Room 108 - Beach View Standard*'). Photos of no
    number go under the room type's name."""
    out: list[tuple[str, list[str]]] = []
    left = list(refs)
    for num in rt.room_list:
        mine = [r for r in left if r in set(rt.number_photos.get(num, []))]
        if mine:
            out.append((f"*Room {num} - {rt.name}*", mine))
            left = [r for r in left if r not in mine]
    if left:
        out.append((f"*{rt.name}*", left))
    return out


def general_photos(prop: Property, sent: set[str]) -> list[str]:
    """The resort's general photos (outside, beach, common areas) not yet sent, in the owner's order, all of them."""
    return [r for r in prop.photo_urls if r not in sent][:MAX_GENERAL_PHOTOS]


# ------------------------------------------------------------------ the plan


def stay_bk(enq: Enquiry, prop: Property, phone: str) -> dict[str, Any] | None:
    """The stay in the shape the room-check helpers take, or None when the dates and persons are not all known."""
    if enq.place != prop.id or not (enq.check_in and enq.check_out and enq.adults):
        return None
    return {"property_id": prop.id, "check_in": enq.check_in.isoformat(), "check_out": enq.check_out.isoformat(),
            "adults": enq.adults, "children": enq.children or 0, "rooms": enq.rooms or 0, "own": phone}


def plan(bot: Any, enq: Enquiry, prop: Property, kind: str, room_type: str, sent: set[str], phone: str) -> Plan:
    """Which photos answer the request, by the owner's rules, never mixed. `problem` is an i18n key when there is
    nothing to send. `unknown_room` (not an i18n key) means the AI named a room that does not exist."""
    rooms = bot.repo.list_room_types(prop.id)  # the owner's priority order
    bk = stay_bk(enq, prop, phone)
    dated = bk is not None
    # every free room type, also when the guest picked one ('other room?' must still find the others)
    free = [rt for rt, _ in bot._fits(prop, {**bk, "pref": None})] if dated else rooms

    def unsent(refs: list[str]) -> list[str]:
        return [r for r in dict.fromkeys(refs) if r not in sent][:MAX_ROOM_PHOTOS]

    picked = {rt.id for p in enq.room_picks if (rt := find_room(bot, rooms, str(p.get("room_type") or "")))}
    offered_id = (enq.checked or {}).get("pref") or (next(iter(picked)) if len(picked) == 1 else None)
    if kind == "room_type":
        named = find_room(bot, rooms, room_type)
        if named is None:
            return Plan(problem="unknown_room", room=room_type)
        if named not in free:
            return Plan(problem="room_not_free", room=named.name)
        refs = unsent(room_refs(bot, named, enq, 1, phone))
        offered = next((rt for rt in free if rt.id == offered_id), free[0] if free else None)
        if dated and (enq.checked or {}).get("available") and offered is not None and named.id != offered.id:
            # another room than the offered one: shown, never switched; its photos already went -> only 'also free'
            return Plan([(named, refs)], also_free=True) if refs else Plan(problem="other_free", room=named.name)
        return Plan([(named, refs)]) if refs else Plan(problem="photos_sent")
    if kind == "resort":
        refs = unsent(prop.photo_urls)
        return Plan([(None, refs)]) if refs else Plan(problem="photos_sent")
    if not free:
        return Plan(problem="no_other_room")
    current = next((rt for rt in free if rt.id == offered_id), free[0])
    refs_of = {rt.id: room_refs(bot, rt, enq, 1, phone) for rt in free}
    others = [rt for rt in free if rt is not current and unsent(refs_of[rt.id])]
    # before any dates every room is shown (owner, 2026-11-21: photos are not restricted); 'not free on your dates' is
    # only said once there ARE dates
    none_left = "no_other_room" if dated else "photos_sent"
    if not dated and not others and not unsent(refs_of[current.id]) and (general := unsent(prop.photo_urls)):
        return Plan([(None, general)])  # every room shown: 'send all photos' / 'more' gets the resort's photos
    other_free = [rt for rt in free if rt is not current]
    if kind == "all_rooms":
        todo = [rt for rt in [current, *others] if unsent(refs_of[rt.id])]
        if todo:
            return Plan([(rt, unsent(refs_of[rt.id])) for rt in todo])
        if dated and other_free:
            return Plan(problem="other_free", room=other_free[0].name)  # 'any other room available?': it IS free
        return Plan(problem=none_left)
    if kind == "other_room":
        if others:
            return Plan([(others[0], unsent(refs_of[others[0].id]))],
                        also_free=dated and bool((enq.checked or {}).get("available")))
        if dated and other_free:
            # another room IS free, its photos already went (or it has none): never 'no other room is free'
            return Plan(problem="other_free", room=other_free[0].name)
        return Plan(problem=none_left)
    # default / more: the offered room, else the next free room not shown yet, else (fix round 4: photos are always
    # allowed) another free room NUMBER of the same types with its own photos, else the resort's photos
    if unsent(refs_of[current.id]):
        return Plan([(current, unsent(refs_of[current.id]))])
    if others:
        return Plan([(others[0], unsent(refs_of[others[0].id]))])
    for rt in [current, *other_free]:
        if refs := more_numbers(bot, rt, enq, sent, phone):
            return Plan([(rt, refs)])
    if general := unsent(prop.photo_urls):
        return Plan([(None, general)])
    return Plan(problem="photos_sent")


def more_numbers(bot: Any, rt: RoomType, enq: Enquiry, sent: set[str], phone: str = "") -> list[str]:
    """The own photos (not sent yet) of the next room of this type that is free on the guest's dates (any room before
    dates), in the owner's order: 'more photos' after the offered room was shown."""
    for number in next_numbers(bot, rt, enq, len(rt.room_list), phone):
        if refs := [r for r in dict.fromkeys(rt.number_photos.get(number, [])) if r not in sent]:
            return refs
    return []


# ------------------------------------------------------------------ queueing


def queue_room(out: Outbox, rt: RoomType, refs: list[str], number: str = "") -> int:
    """The room's label as a text, then its photos (owner, 2026-11-20: a text between two rooms' photos says which room
    and number they are). The label names the room the guest would get (`number`); photos of several room numbers are
    split under each number's own label. A label goes even with no photo. Returns the number of photos queued."""
    sets = label_sets(rt, refs)
    if len(sets) <= 1 and not (sets and sets[0][0].startswith("*Room ")):  # one room's own photos keep their number
        sets = [(room_label(rt, number), list(refs))]
    count = 0
    for label, part in sets:
        out.text("room_label", label)
        if part:
            out.photos(f"photos:{len(part)}", part)
            count += len(part)
    return count


def queue_steps(out: Outbox, steps: list[tuple[RoomType | None, list[str]]], bot: Any = None,
                enq: Enquiry | None = None, phone: str = "") -> int:
    count = 0
    for rt, refs in steps:
        if rt is None:
            out.photos(f"photos:{len(refs)}", refs)  # general photos go without a label
            count += len(refs)
        else:
            number = next(iter(next_numbers(bot, rt, enq, 1, phone)), "") if bot is not None and enq is not None else ""
            count += queue_room(out, rt, refs, number)
    return count


def open_step(bot: Any, enq: Enquiry, lang: str) -> tuple[str, str] | None:
    """The booking step still open, as the owner's text: the location question, the form with the missing lines, or the
    name and mobile ask. None when nothing is waiting for the guest."""
    step = enq.next_step
    if step == "ask_location":
        return "ask_location", t("ask_location", lang, towns=bot._towns_text())
    if step == "ask_details" and enq.missing:
        got = {"check_in": bool(enq.check_in), "check_out": bool(enq.check_out), "persons": bool(enq.adults),
               "rooms": enq.rooms or 0}
        return "form", bot._missing_form(lang, got)
    if step == "ask_extra_bed":
        key = "ask_extra_beds" if int((enq.checked or {}).get("extra_bed_max") or 1) > 1 else "ask_extra_bed"
        return key, t(key, lang)
    if step == "ask_contact":
        return "ask_contact", t("ask_contact", lang)
    return None


def queue_open_step(bot: Any, phone: str, enq: Enquiry, lang: str, out: Outbox, *, dates_line: bool = True) -> None:
    """After the photos the guest is taken back to the booking: before any dates the reason (once per conversation),
    then the step still open, never the same text twice in a row."""
    step = open_step(bot, enq, lang)
    if step is None:
        return
    key, text = step
    if dates_line and key == "form" and not enq.check_in:
        line = t("dates_for_availability", lang)
        if not already_said(bot, phone, out, line):
            out.text("dates_for_availability", line)
    if text != last_text(bot, phone, out):
        out.text(key, text)
