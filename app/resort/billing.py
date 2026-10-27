"""The guest's bill (folio), payments parsed from the history, invoice numbers and the collection report.

Pure functions: they take bookings, charges and refunds and return numbers, so they are easy to test and
the dashboard, invoices and reports all agree.

Money rules (rupees):
    bill_total = booking.total + sum(charges)      (charges are entered tax-inclusive; negative = discount / waiver)
    net_paid   = amount paid - sum(refunds)
    balance    = max(bill_total - net_paid, 0)     (a cancelled booking owes nothing)
    credit     = max(net_paid - bill_total, 0)
GST is already inside the prices: gst = bill_total * pct / (100 + pct), split half CGST / half SGST.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from app.core.clock import IST
from app.resort.models import Booking, Charge, Property, Refund

PAID_STATES = ("paid", "checked_in", "checked_out")  # a guest who has paid (or stayed)
IN_HOUSE_STATES = ("paid", "checked_in")  # still owe a stay or are staying

CATEGORY_LABELS = {"food": "Food", "activity": "Activity", "laundry": "Laundry", "extra_bed": "Extra bed",
                   "transport": "Transport", "other": "Other", "discount": "Discount / waiver"}


@dataclass(frozen=True)
class FolioLine:
    kind: str  # room | charge | discount
    label: str
    detail: str
    amount: int
    charge_id: str = ""
    at: datetime | None = None
    by: str = ""


@dataclass(frozen=True)
class Folio:
    lines: list[FolioLine]
    room_total: int
    charges_total: int
    bill_total: int
    subtotal: int  # bill without GST
    gst: int
    cgst: int
    sgst: int
    gst_pct: int
    paid: int
    refunded: int
    net_paid: int
    balance: int  # still to pay (0 for a cancelled booking)
    credit: int  # paid more than the bill
    retained: int  # cancelled booking: paid money the resort keeps (cancellation fee)


def split_gst(amount: int, pct: int) -> tuple[int, int, int, int]:
    """(excl. GST, GST, CGST, SGST) of a tax-inclusive amount."""
    if pct <= 0 or amount == 0:
        return amount, 0, 0, 0
    gst = round(amount * pct / (100 + pct))
    cgst = gst // 2
    return amount - gst, gst, cgst, gst - cgst


def folio(b: Booking, charges: list[Charge], refunds: list[Refund], prop: Property | None,
          room_name: str = "") -> Folio:
    pct = prop.gst_pct if prop else 0
    nights = b.nights
    label = f"{room_name or 'Room'} x {b.rooms}" if room_name else f"Room x {b.rooms}"
    lines = [FolioLine("room", "Room rent", f"{label}, {nights} night{'s' if nights != 1 else ''}"
                       f" ({b.check_in:%d %b} to {b.check_out:%d %b %Y})", b.total)]
    for c in sorted(charges, key=lambda c: c.at):
        kind = "discount" if c.amount < 0 or c.category == "discount" else "charge"
        lines.append(FolioLine(kind, CATEGORY_LABELS.get(c.category, c.category.title()), c.description, c.amount,
                               c.id, c.at, c.added_by))
    charges_total = sum(c.amount for c in charges)
    bill_total = b.total + charges_total
    subtotal, gst, cgst, sgst = split_gst(bill_total, pct)
    paid, refunded = b.paid_amount, sum(r.amount for r in refunds)
    net_paid = paid - refunded
    cancelled = b.status == "cancelled"
    balance = 0 if cancelled else max(bill_total - net_paid, 0)
    credit = 0 if cancelled else max(net_paid - bill_total, 0)
    return Folio(lines, b.total, charges_total, bill_total, subtotal, gst, cgst, sgst, pct, paid, refunded, net_paid,
                 balance, credit, max(net_paid, 0) if cancelled else 0)


# --------------------------------------------------------------------------- payments from the history

_LINE = re.compile(r"^\[(?P<at>[^\]]+)\]\s+(?P<method>.+?)\s+₹(?P<amount>[\d,]+)\s+·\s+ref\s+(?P<ref>\S+)"
                   r"(?:\s+·\s+by\s+(?P<by>\S+))?(?:\s+·\s+(?P<note>.*))?$")
_METHOD_KEYS = {"cash": "cash", "upi": "upi", "card": "card", "bank transfer": "bank_transfer",
                "online (razorpay)": "online", "mixed": "mixed"}


@dataclass(frozen=True)
class Payment:
    n: int  # 1-based, the receipt number within the booking
    at: datetime | None
    method: str  # key: cash | upi | card | bank_transfer | online
    amount: int
    ref: str
    by: str = ""
    note: str = ""


def _parse_stamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.replace(" IST", ""), "%d %b %Y %H:%M").replace(tzinfo=IST)
    except ValueError:
        return None


def payments_of(b: Booking) -> list[Payment]:
    """Every payment on the booking, read from the history lines. Older / online payments with no history
    line become one payment built from the booking's own fields."""
    out: list[Payment] = []
    for line in b.payment_note.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        out.append(Payment(len(out) + 1, _parse_stamp(m["at"]), _METHOD_KEYS.get(m["method"].lower(), "cash"),
                           int(m["amount"].replace(",", "")), m["ref"], m["by"] or "", (m["note"] or "").strip()))
    if not out and b.paid_amount:
        out.append(Payment(1, b.paid_at, b.payment_method or "online", b.paid_amount,
                           b.payment_ref or b.razorpay_payment_id, b.received_by))
    elif out and sum(p.amount for p in out) < b.paid_amount:  # e.g. an online advance, then desk payments
        missing = b.paid_amount - sum(p.amount for p in out)
        out.insert(0, Payment(1, b.paid_at, "online", missing, b.razorpay_payment_id or b.payment_ref, ""))
        out = [Payment(i + 1, p.at, p.method, p.amount, p.ref, p.by, p.note) for i, p in enumerate(out)]
    return out


def invoice_number(b: Booking, prop: Property | None) -> str:
    prefix = (prop.invoice_prefix if prop and prop.invoice_prefix else "INV").strip() or "INV"
    year = b.created_at.astimezone(IST).year if b.created_at else date.today().year
    return f"{prefix}-{year}-{b.id.split('-')[-1]}"


# --------------------------------------------------------------------------- collection report


@dataclass
class Collections:
    start: date
    end: date
    by_day: dict[date, dict[str, int]] = field(default_factory=dict)  # day -> method -> rupees (refunds negative)
    by_method: dict[str, int] = field(default_factory=dict)
    by_staff: dict[str, int] = field(default_factory=dict)
    collected: int = 0
    refunded: int = 0
    rows: list[dict] = field(default_factory=list)  # one line per payment / refund, for the table and CSV

    @property
    def net(self) -> int:
        return self.collected - self.refunded


def collections(bookings: list[Booking], refunds: list[Refund], start: date, end: date) -> Collections:
    """Money received and refunded between two IST dates (both included)."""
    out = Collections(start, end)
    by_id = {b.id: b for b in bookings}

    def add(day: date, method: str, staff: str, amount: int) -> None:
        out.by_day.setdefault(day, {})
        out.by_day[day][method] = out.by_day[day].get(method, 0) + amount
        out.by_method[method] = out.by_method.get(method, 0) + amount
        out.by_staff[staff] = out.by_staff.get(staff, 0) + amount

    for b in bookings:
        for p in payments_of(b):
            at = p.at or b.paid_at
            if not at:
                continue
            day = at.astimezone(IST).date()
            if start <= day <= end:
                staff = p.by or ("Razorpay (automatic)" if p.method == "online" else "—")
                add(day, p.method, staff, p.amount)
                out.collected += p.amount
                out.rows.append({"at": at, "kind": "payment", "booking": b.id, "guest": b.guest_name or b.guest_phone,
                                 "method": p.method, "amount": p.amount, "ref": p.ref, "by": staff})
    for r in refunds:
        b = by_id.get(r.booking_id)
        day = r.at.astimezone(IST).date()
        if b is None or not (start <= day <= end):
            continue
        add(day, r.method, r.by or "—", -r.amount)
        out.refunded += r.amount
        out.rows.append({"at": r.at, "kind": "refund", "booking": b.id, "guest": b.guest_name or b.guest_phone,
                         "method": r.method, "amount": -r.amount, "ref": r.ref, "by": r.by or "—"})
    out.rows.sort(key=lambda row: row["at"])
    return out
