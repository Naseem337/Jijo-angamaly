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


# --------------------------------------------------------------------------- price


def night_rate(rt: RoomType, rules: list[RateRule], night: date) -> int:
    """Rate for one night of one room: season rule > weekend rate > base rate."""
    for rule in rules:
        if rule.start <= night <= rule.end:
            return rule.rate
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
