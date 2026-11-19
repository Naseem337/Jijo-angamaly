"""Pure booking rules: who fits in how many rooms, what a stay costs, what is free.

No I/O here besides the ResortRepo calls passed in, so everything is easy to test. The bot and the
dashboard both call these functions; the AI never decides a price, a capacity or availability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.resort.models import BLOCKING, Booking, Property, RateRule, RoomType

MAX_GUESTS = 40
MAX_NIGHTS = 30


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


def base_per_room(rt: RoomType, beds_ok: bool = True) -> int:
    """Guests a room takes WITHOUT an extra bed (owner, 2026-11-19: 2 per room; the 3rd needs an extra bed). With
    extra beds offered, 'max guests per room' in the dashboard is the normal guests + 1 extra bed."""
    return max(1, rt.max_occupancy - 1) if beds_ok and rt.max_occupancy >= 2 else max(1, rt.max_occupancy)


def plan_rooms(rt: RoomType, adults: int, children: int, free: int, wanted: int = 0,
               beds_ok: bool = True) -> tuple[int, int] | None:
    """(rooms, extra beds) for the party in this room type, or None if it can't (owner, 2026-11-19):
    - normally enough rooms for the normal guests per room (8 persons -> 4 rooms), no extra beds;
    - FEWER rooms only when the guest asks for that number: then up to 'max guests per room' each, the extra persons
      on extra beds (8 persons in 3 rooms -> 2 extra beds)."""
    total = adults + children
    if adults < 1 or children < 0 or rt.max_adults < 1 or rt.max_occupancy < 1:
        return None
    if children and rt.max_children < 1:
        return None
    base = base_per_room(rt, beds_ok)
    need = max(-(-adults // rt.max_adults), -(-children // rt.max_children) if children else 1)
    if wanted:
        lowest = max(need, -(-total // rt.max_occupancy))
        if not (lowest <= wanted <= min(free, adults)):
            return None
        return wanted, max(0, total - base * wanted) if beds_ok else 0
    rooms = min(max(need, -(-total // base)), adults)
    if rooms > free or total > rooms * rt.max_occupancy:
        return None
    return rooms, max(0, total - base * rooms) if beds_ok else 0


def fewest_rooms(rts: list[RoomType], adults: int, children: int) -> int:
    """The fewest rooms any room type could take the party in, with extra beds (for 'at least N rooms')."""
    total = adults + children
    return min((max(-(-total // rt.max_occupancy), -(-adults // max(rt.max_adults, 1))) for rt in rts), default=0)


def party_fits(rooms: list[RoomType], adults: int, children: int, beds_ok: bool = True) -> bool:
    """Can these rooms (one entry per room) hold the party at the normal guests per room (no extra beds), with at
    least one adult in each room?"""
    return (len(rooms) <= adults and adults <= sum(r.max_adults for r in rooms)
            and adults + children <= sum(base_per_room(r, beds_ok) for r in rooms)
            and children <= sum(r.max_children for r in rooms))


def mixed_rooms(free: list[tuple[RoomType, int]], adults: int, children: int,
                wanted: int = 0, beds_ok: bool = True) -> list[tuple[RoomType, int]] | None:
    """The owner's priority across room types (2026-11-19): the fewest rooms (or `wanted` rooms) taken from the top of
    the priority list, e.g. 1 Basic View (the last one free) + 1 Standard for 4 guests. `free` is (room type, free
    rooms) in priority order. Returns [(room type, rooms), ...] or None if no set of rooms fits."""
    units = [rt for rt, n in free for _ in range(max(n, 0)) if rt.max_adults >= 1 and rt.max_occupancy >= 1]
    for n in ([wanted] if wanted else range(1, min(len(units), adults) + 1)):
        pick = units[:n]
        # the guest asked for this number of rooms: extra beds may take the 3rd person (owner, 2026-11-19)
        if len(pick) == n and (party_fits(pick, adults, children, beds_ok) or (
                wanted and beds_ok and party_fits(pick, adults, children, False))):
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
    if (check_out - check_in).days > MAX_NIGHTS:
        return f"stays are limited to {MAX_NIGHTS} nights"
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
