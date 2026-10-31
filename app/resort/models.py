"""Resort data model: properties, room types, rate rules, bookings, guests.

Money is whole rupees (int). Dates are `date` (check_out is the morning the guest leaves,
so a stay of check_in..check_out covers the nights check_in, ..., check_out - 1).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import date, datetime

BOOKING_STATUSES = ("hold", "paid", "checked_in", "checked_out", "failed", "expired", "cancelled")
# Statuses that keep rooms blocked. A hold only blocks until hold_expires_at.
BLOCKING = ("hold", "paid", "checked_in", "checked_out")


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3).upper()}"


@dataclass
class Property:
    name: str
    city: str = ""
    place: str = ""  # how guests say it: "Kanthalloor, Munnar". Shown in the bot's first question
    description: str = ""
    checkin_time: str = "14:00"
    checkout_time: str = "11:00"
    advance_pct: int = 50  # % of the total paid online to confirm
    gst_pct: int = 0  # 0 = rates already include GST
    address: str = ""
    map_url: str = ""
    photo_urls: list[str] = field(default_factory=list)
    staff_numbers: list[str] = field(default_factory=list)  # E.164, get the handoff / booking alerts
    contact_numbers: list[str] = field(default_factory=list)  # E.164, the owner's numbers the bot gives to guests
    extra_bed_rate: int = 0  # per bed per night, rupees; 0 = no extra beds offered
    highlights: list[str] = field(default_factory=list)  # short selling points: "Infinity pool", "Mountain view"
    policies: str = ""  # cancellation / refund / house rules, in the resort's own words
    gstin: str = ""  # shown on invoices
    invoice_prefix: str = "INV"
    # Refund of the amount paid, by days left before check-in (first row whose days_before <= days left wins).
    cancel_policy: list[dict] = field(default_factory=lambda: [
        {"days_before": 7, "refund_pct": 100}, {"days_before": 3, "refund_pct": 50}, {"days_before": 0, "refund_pct": 0}])
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
    payment_method: str = ""  # online (Razorpay) | cash | upi | card | bank_transfer
    payment_ref: str = ""  # transaction / receipt / UTR number
    amount_paid: int = 0  # rupees actually received so far
    payment_note: str = ""
    received_by: str = ""  # staff email for cash / manual payments
    checked_in_at: datetime | None = None
    checked_out_at: datetime | None = None
    room_numbers: str = ""  # physical rooms given at check-in, e.g. "101, 102"
    cancel_reason: str = ""
    notes: str = ""
    id: str = field(default_factory=lambda: new_id("RB"))

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def paid_amount(self) -> int:
        """Rupees received so far (older paid bookings didn't store it: they paid the advance)."""
        return self.amount_paid or (self.advance if self.status == "paid" else 0)

    @property
    def balance(self) -> int:
        """Still to pay at the property."""
        return max(self.total - self.paid_amount, 0)


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


CHARGE_CATEGORIES = ("food", "activity", "laundry", "extra_bed", "transport", "other")


@dataclass
class Charge:
    """An extra on the guest's bill (food, activity, laundry...). Room rent is the booking total."""

    booking_id: str
    description: str
    amount: int  # rupees, always positive
    at: datetime
    category: str = "other"
    added_by: str = ""
    id: str = field(default_factory=lambda: new_id("CH"))


@dataclass
class Refund:
    booking_id: str
    amount: int  # rupees
    at: datetime
    method: str = "cash"  # cash | upi | bank_transfer | online
    ref: str = ""
    note: str = ""
    by: str = ""
    id: str = field(default_factory=lambda: new_id("RF"))


@dataclass
class IdProof:
    """A guest's ID photo sent on WhatsApp. `data` is the image; only the last 4 digits are kept as text."""

    booking_id: str
    guest_no: int  # 1 = lead guest, 2 = next adult ...
    id_type: str  # aadhaar | passport | driving_licence | voter_id | other | later
    received_at: datetime
    last4: str = ""
    mime: str = ""
    data: bytes = b""
    status: str = "received"  # received | verified | rejected | pending (promised for check-in)
    verified_by: str = ""
    delete_after: date | None = None
    id: str = field(default_factory=lambda: new_id("ID"))
