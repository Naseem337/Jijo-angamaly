"""Pure booking rules: who fits in how many rooms, what a stay costs, what is free.

No I/O here besides the ResortRepo calls passed in, so everything is easy to test. The bot and the
dashboard both call these functions; the AI never decides a price, a capacity or availability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.resort.models import BLOCKING, Booking, Property, RateRule, RoomType



# --------------------------------------------------------------------------- capacity


def min_rooms(rt: RoomType, adults: int, children: int) -> int | None:
    """Fewest rooms of this type that can hold the party, or None if one party can't fit
    in this room type at all (e.g. no children allowed but children came)."""
    if adults < 1 or children < 0 or rt.max_adults < 1 or rt.max_occupancy < 1:
        return None
    if children and rt.max_children < 1:
        return None
    return max(
        -(-adults // rt.max_adults),  # ceil division
        -(-(adults + children) // rt.max_occupancy),
        -(-children // rt.max_children) if children else 1,
    )


def room_options(rt: RoomType, adults: int, children: int, available_units: int) -> list[int]:
    """Room counts the guest may pick: from the minimum needed up to what is free and sensible
    (each room needs at least one adult). 10 people in one room is never offered."""
    low = min_rooms(rt, adults, children)
    if low is None:
        return []
    high = min(available_units, adults)
    return list(range(low, high + 1)) if low <= high else []


# The owner's room rule (2026-11-24): persons are ADULTS above 6 years only (children are not asked and not counted).
# 2 adults per room by default (the fewest rooms at 2 per room); the guest may choose fewer rooms, up to 3 adults in one
# room; never more. An extra bed is never added by itself: it is asked once, only when a room will hold 3 adults.
BASE_ADULTS_PER_ROOM = 2
MAX_ADULTS_PER_ROOM = 3


def room_cap(rt: RoomType) -> int:
    """The most adults one room of this type takes (3, or the dashboard's 'max guests per room' when that is lower)."""
    return max(1, min(MAX_ADULTS_PER_ROOM, rt.max_occupancy))


def base_per_room(rt: RoomType, beds_ok: bool = True) -> int:
    """Adults a room takes by default: 2. (beds_ok is kept only so old callers still work.)"""
    return max(1, min(BASE_ADULTS_PER_ROOM, rt.max_occupancy))


def rooms_with_three(adults: int, rooms: int) -> int:
    """How many rooms will hold 3 adults when `adults` are spread as evenly as possible over `rooms` rooms. Only then is
    the extra-bed question asked: 3 adults in 2 rooms is 2 + 1 (none), 5 in 2 rooms is 3 + 2 (one), 3 in 1 room is one."""
    if rooms < 1:
        return 0
    low, extra = divmod(adults, rooms)  # `extra` rooms hold low + 1 adults, the others low
    return (extra if low + 1 >= 3 else 0) + ((rooms - extra) if low >= 3 else 0)


def plan_rooms(rt: RoomType, adults: int, children: int, free: int, wanted: int = 0,
               beds_ok: bool = True) -> tuple[int, int] | None:
    """(rooms, extra beds) for the adults in this room type, or None if it can't. Extra beds are never planned (0):
    - normally enough rooms for 2 adults each (8 adults -> 4 rooms);
    - FEWER rooms only when the guest asks for that number: then up to 3 adults per room (8 adults in 3 rooms)."""
    if adults < 1 or rt.max_occupancy < 1:
        return None
    cap, base = room_cap(rt), base_per_room(rt)
    if wanted:
        if not (-(-adults // cap) <= wanted <= min(free, adults)):
            return None
        return wanted, 0
    rooms = min(-(-adults // base), adults)
    if rooms > free or adults > rooms * cap:
        return None
    return rooms, 0


def fewest_rooms(rts: list[RoomType], adults: int, children: int) -> int:
    """The fewest rooms any room type could take the adults in, at 3 per room (for 'at least N rooms')."""
    return min((-(-adults // room_cap(rt)) for rt in rts), default=0)


def party_fits(rooms: list[RoomType], adults: int, children: int, by_base: bool = True) -> bool:
    """Can these rooms (one entry per room) hold the adults at 2 per room (by_base) or at 3 per room, with at least one
    adult in each room?"""
    per = base_per_room if by_base else room_cap
    return len(rooms) <= adults and adults <= sum(per(r) for r in rooms)


def mixed_rooms(free: list[tuple[RoomType, int]], adults: int, children: int,
                wanted: int = 0, beds_ok: bool = True) -> list[tuple[RoomType, int]] | None:
    """The owner's priority across room types (2026-11-19): the fewest rooms (or `wanted` rooms) taken from the top of
    the priority list, e.g. 1 Basic View (the last one free) + 1 Standard for 4 guests. `free` is (room type, free
    rooms) in priority order. Returns [(room type, rooms), ...] or None if no set of rooms fits."""
    units = [rt for rt, n in free for _ in range(max(n, 0)) if rt.max_adults >= 1 and rt.max_occupancy >= 1]
    for n in ([wanted] if wanted else range(1, min(len(units), adults) + 1)):
        pick = units[:n]
        # the guest asked for this number of rooms: up to 3 adults per room (owner, 2026-11-24)
        if len(pick) == n and (party_fits(pick, adults, children) or (wanted and party_fits(pick, adults, children, False))):
            out: list[tuple[RoomType, int]] = []
            for rt in pick:
                if out and out[-1][0] is rt:
                    out[-1] = (rt, out[-1][1] + 1)
                else:
                    out.append((rt, 1))
            return out
    return None


# --------------------------------------------------------------------------- price


def night_rate(rt: RoomType, rules: list[RateRule], night: date) -> int:
    """Rate for one night of one room: season rule > weekend rate > base rate. When season prices overlap, the
    shortest date range wins (a 2-day festival inside a 3-week season), and the one added last breaks a tie."""
    matching = [(rule.end - rule.start, -i, rule) for i, rule in enumerate(rules) if rule.start <= night <= rule.end]
    if matching:
        return min(matching, key=lambda m: (m[0], m[1]))[2].rate
    if rt.weekend_rate is not None and night.weekday() in (4, 5):  # Fri, Sat
        return rt.weekend_rate
    return rt.base_rate


@dataclass(frozen=True)
class Quote:
    nights: int
    rooms: int
    subtotal: int
    gst: int
    total: int
    advance: int
    per_night: list[tuple[date, int]]


def quote(prop: Property, rt: RoomType, rules: list[RateRule], check_in: date, check_out: date,
          rooms: int, extra_beds: int = 0) -> Quote:
    """Room rent for every night plus `extra_beds` at the property's per-bed-per-night rate."""
    nights = (check_out - check_in).days
    per_night = [(check_in + timedelta(days=i), night_rate(rt, rules, check_in + timedelta(days=i)))
                 for i in range(nights)]
    subtotal = sum(rate for _, rate in per_night) * rooms + extra_beds * prop.extra_bed_rate * nights
    gst = round(subtotal * prop.gst_pct / 100)
    total = subtotal + gst
    advance = round(total * prop.advance_pct / 100)
    return Quote(nights, rooms, subtotal, gst, total, advance, per_night)


# --------------------------------------------------------------------------- availability


def booking_blocks(b: Booking, now: datetime) -> bool:
    if b.status not in BLOCKING:
        return False
    if b.status == "hold":
        return b.hold_expires_at is not None and b.hold_expires_at > now
    return True


def free_units(rt: RoomType, bookings: list[Booking], check_in: date, check_out: date, now: datetime) -> int:
    """Rooms of this type free on EVERY night of the stay."""
    worst = 0
    day = check_in
    while day < check_out:
        used = sum(b.rooms for b in bookings
                   if b.room_type_id == rt.id and booking_blocks(b, now) and b.check_in <= day < b.check_out)
        worst = max(worst, used)
        day += timedelta(days=1)
    return max(rt.units - worst, 0)


def split_rooms(text: str) -> list[str]:
    """'101, 102' or one per line -> ['101', '102'] (no blanks, no repeats, order kept)."""
    out: list[str] = []
    for part in text.replace("\n", ",").split(","):
        if part.strip() and part.strip() not in out:
            out.append(part.strip())
    return out


def allocate_rooms(rt: RoomType, b: Booking, others: list[Booking], now: datetime) -> list[str]:
    """The highest-priority rooms of rt.room_list that no other live booking holds on any night of b's stay.
    Returns at most b.rooms numbers (fewer if the list is short or the rooms are taken)."""
    taken = {num for o in others
             if o.id != b.id and o.room_type_id == rt.id and booking_blocks(o, now)
             and o.check_in < b.check_out and o.check_out > b.check_in
             for num in split_rooms(o.room_numbers)}
    return [num for num in rt.room_list if num not in taken][:b.rooms]


def check_dates(check_in: date, check_out: date, today: date) -> str | None:
    """Why these dates can't be booked, or None. Messages are for the bot's logic, not shown raw."""
    if check_in < today:
        return "check-in is in the past"
    if check_out <= check_in:
        return "check-out must be after check-in"
    return None


def rate_summary(per_night: list[tuple[date, int]]) -> str:
    """'2 nights x Rs 2,500, 3 nights x Rs 3,000' (consecutive nights at one rate are grouped)."""
    groups: list[list[int]] = []
    for _, rate in per_night:
        if groups and groups[-1][0] == rate:
            groups[-1][1] += 1
        else:
            groups.append([rate, 1])
    return ", ".join(f"{n} night{'s' if n != 1 else ''} x Rs {rate:,}" for rate, n in groups)


def fmt_range(start: date, end: date) -> str:
    """'1 Dec to 14 Dec 2026'."""
    return f"{start.day} {start.strftime('%b')} to {end.day} {end.strftime('%b')} {end.year}"
