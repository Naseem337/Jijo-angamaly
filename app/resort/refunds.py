"""Cancelling a booking and recording refunds.

The refund itself is money the resort hands back by hand (cash, UPI, bank transfer, or a Razorpay refund done in
Razorpay's own dashboard): this module only records it, so the folio and the collection report stay right.
"""

from __future__ import annotations

from datetime import date, datetime

from app.core.clock import IST
from app.resort.db import ResortRepo
from app.resort.models import Booking, Property, Refund

REFUND_METHODS = {"cash": "Cash", "upi": "UPI", "bank_transfer": "Bank transfer",
                  "online": "Online (refunded in Razorpay)"}
NEEDS_REF = ("upi", "bank_transfer")


class RefundError(ValueError):
    """Bad cancellation / refund details; the text is shown to staff."""


def refundable(b: Booking, refunds: list[Refund]) -> int:
    """Paid money not yet handed back."""
    return max(b.paid_amount - sum(r.amount for r in refunds), 0)


def suggested_refund(prop: Property | None, b: Booking, today: date, refunds: list[Refund] | None = None) -> tuple[int, int]:
    """(percent, rupees) the cancellation policy gives back, from the days left before check-in."""
    days_left = (b.check_in - today).days
    rows = sorted((r for r in (prop.cancel_policy if prop else []) if isinstance(r, dict)),
                  key=lambda r: -int(r.get("days_before", 0)))
    pct = 0
    for row in rows:
        if days_left >= int(row.get("days_before", 0)):
            pct = max(0, min(100, int(row.get("refund_pct", 0))))
            break
    return pct, min(round(b.paid_amount * pct / 100), refundable(b, refunds or []))


def record_refund(repo: ResortRepo, b: Booking, *, amount: int, method: str, ref: str = "", note: str = "",
                  by: str = "", now: datetime) -> Refund:
    if method not in REFUND_METHODS:
        raise RefundError("Choose how the refund was paid")
    if amount < 1:
        raise RefundError("Enter the refund amount")
    left = refundable(b, repo.list_refunds(b.id))
    if amount > left:
        raise RefundError(f"Only {left:,} rupees can still be refunded on this booking")
    ref = ref.strip()
    if method in NEEDS_REF and not ref:
        raise RefundError(f"Enter the {REFUND_METHODS[method]} reference / transaction id")
    return repo.add_refund(Refund(b.id, amount, now, method, ref or f"RFND-{b.id}-{len(repo.list_refunds(b.id)) + 1}",
                                  note.strip(), by))


def cancel_booking(repo: ResortRepo, b: Booking, *, reason: str, refund_amount: int = 0, method: str = "cash",
                   ref: str = "", note: str = "", by: str = "", now: datetime) -> Refund | None:
    """Cancel (rooms are freed because a cancelled booking blocks nothing) and record the refund, if any."""
    if b.status == "cancelled":
        raise RefundError("This booking is already cancelled")
    if b.status in ("checked_in", "checked_out"):
        raise RefundError("The guest has checked in: check them out instead of cancelling")
    if not reason.strip():
        raise RefundError("Write the reason for cancelling")
    refund = record_refund(repo, b, amount=refund_amount, method=method, ref=ref, note=note, by=by,
                           now=now) if refund_amount > 0 else None
    b.status = "cancelled"
    b.cancel_reason = reason.strip()
    stamp = now.astimezone(IST).strftime("%d %b %H:%M")
    b.notes = (b.notes + f"\n[{stamp}] Cancelled by {by or 'staff'}: {reason.strip()}").strip()
    repo.save_booking(b)
    return refund
