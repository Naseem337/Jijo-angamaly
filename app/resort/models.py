"""Resort data model: properties, room types, rate rules, bookings, guests.

Money is whole rupees (int). Dates are `date` (check_out is the morning the guest leaves,
so a stay of check_in..check_out covers the nights check_in, ..., check_out - 1).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import date, datetime

BOOKING_STATUSES = ("hold", "paid", "failed", "expired", "cancelled")
# Statuses that keep rooms blocked. A hold only blocks until hold_expires_at.
BLOCKING = ("hold", "paid")


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3).upper()}"


@dataclass
class Property:
    name: str
    city: str = ""
    description: str = ""
    checkin_time: str = "14:00"
    checkout_time: str = "11:00"
    advance_pct: int = 50  # % of the total paid online to confirm
    gst_pct: int = 0  # 0 = rates already include GST
    address: str = ""
    map_url: str = ""
    photo_urls: list[str] = field(default_factory=list)
    staff_numbers: list[str] = field(default_factory=list)  # E.164, get the handoff / booking alerts
    policies: str = ""  # cancellation / refund / house rules, in the resort's own words
    active: bool = True
    id: str = field(default_factory=lambda: new_id("P"))


@dataclass
class RoomType:
    property_id: str
    name: str
    units: int = 1  # how many of these rooms the property has
    max_adults: int = 2  # per room
    max_children: int = 1  # per room
    max_occupancy: int = 3  # adults + children per room
    base_rate: int = 0  # per room per night, rupees
    weekend_rate: int | None = None  # Fri and Sat nights, if different
    description: str = ""
    amenities: str = ""
    photo_urls: list[str] = field(default_factory=list)
    active: bool = True
    id: str = field(default_factory=lambda: new_id("RT"))


@dataclass
class RateRule:
    """Season / festival price: nights in [start, end] (both inclusive) cost `rate` per room."""

    room_type_id: str
    start: date
    end: date
    rate: int
    label: str = ""
    id: str = field(default_factory=lambda: new_id("RR"))


@dataclass
class Booking:
    property_id: str
    room_type_id: str
    guest_phone: str
    check_in: date
    check_out: date
    adults: int
    children: int
    rooms: int
    total: int  # for the whole stay, rupees
    advance: int  # to pay now, rupees
    created_at: datetime
    status: str = "hold"
    guest_name: str = ""
    language: str = "en"
    hold_expires_at: datetime | None = None
    paid_at: datetime | None = None
    payment_link_id: str = ""
    payment_link_url: str = ""
    razorpay_payment_id: str = ""
    notes: str = ""
    id: str = field(default_factory=lambda: new_id("RB"))

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def balance(self) -> int:
        return self.total - self.advance if self.status == "paid" else self.total


@dataclass
class Guest:
    phone: str
    name: str = ""
    language: str = "en"
    offers_consent: bool = False  # said yes to offers / alerts
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    notes: str = ""


@dataclass
class ChatMessage:
    phone: str
    direction: str  # "in" | "out"
    text: str
    at: datetime
    source: str = "text"  # text | voice | button | system | staff


@dataclass
class DashboardUser:
    email: str
    password_hash: str
    name: str = ""
    role: str = "owner"  # owner | manager
    property_ids: list[str] = field(default_factory=list)  # empty = all properties
    active: bool = True
