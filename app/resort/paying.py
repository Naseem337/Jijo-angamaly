"""Recording money received for a booking (Razorpay online, or cash / UPI / card / bank transfer at the desk).

One function so the Razorpay webhook and the dashboard fill the same fields the same way.
"""

from __future__ import annotations

from datetime import datetime

from app.core.clock import IST
from app.resort.models import Booking

METHODS = {  # key -> label shown in the dashboard
    "cash": "Cash",
    "upi": "UPI",
    "card": "Card",
    "bank_transfer": "Bank transfer",
    "online": "Online (Razorpay)",
    "mixed": "Mixed",
}
DESK_METHODS = ("cash", "upi", "card", "bank_transfer")  # staff can record these
NEEDS_REF = ("upi", "card", "bank_transfer")  # a transaction id is required


class PaymentError(ValueError):
    """Bad payment details; the text is shown to staff."""


def method_label(method: str) -> str:
    return METHODS.get(method, method.replace("_", " ").title() if method else "—")


def history(b: Booking) -> list[str]:
    return [line for line in b.payment_note.splitlines() if line.strip()]


def apply_payment(b: Booking, *, method: str, amount: int, ref: str = "", note: str = "", received_by: str = "",
                  now: datetime, extra: int = 0) -> str:
    """Add a payment to `b` (not saved here). Returns the receipt / reference id stored for it.

    The first payment makes the booking paid; later ones (the balance) just add up. A blank cash
    reference becomes a receipt number like RCPT-RB-AB12CD-1. `extra` is the total of the guest's extra charges,
    so the final balance (room + extras) can be paid.
    """
    if method not in METHODS or method == "mixed":
        raise PaymentError("Choose how the money was received")
    if amount < 1:
        raise PaymentError("Enter the amount received")
    remaining = b.total + extra - b.paid_amount
    if amount > remaining:
        raise PaymentError(f"Only ₹{remaining:,} is still due on this booking")
    ref = ref.strip()
    if method in NEEDS_REF and not ref:
        raise PaymentError(f"Enter the {method_label(method)} reference / transaction id")
    first = b.paid_amount == 0
    count = len(history(b)) + 1
    if not ref:
        ref = f"RCPT-{b.id}-{count}"
    stamp = now.astimezone(IST).strftime("%d %b %Y %H:%M IST")
    line = f"[{stamp}] {method_label(method)} ₹{amount:,} · ref {ref}" + (f" · by {received_by}" if received_by else "")
    if note.strip():
        line += f" · {note.strip()}"
    b.payment_note = "\n".join([*history(b), line])
    b.amount_paid = b.paid_amount + amount
    b.payment_method = method if first or b.payment_method == method else "mixed"
    b.payment_ref = ref
    if method != "online" and received_by:
        b.received_by = received_by
    if b.status in ("hold", "expired", "failed"):  # a checked-in guest paying the balance stays checked in
        b.status, b.paid_at, b.hold_expires_at = "paid", now, None
    return ref
