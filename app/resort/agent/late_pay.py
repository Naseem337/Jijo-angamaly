"""Payments that arrive after our hold ended (owner, 2026-11-27: the hold is 9 minutes, Razorpay links live at least 15).

The sweep closes the link at 9 minutes, but a payment already started on Razorpay's page can still go through. Then
the rooms are checked again: still free -> confirmed as normal (bot.mark_paid). Taken by someone else -> NOT confirmed:
the team is alerted (refund or rebook) and the guest gets the one allowed promise, that the team will contact them.
The old flow.py is not changed; the webhook and check_payment call this in agent mode.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

LATE_MARK = "PAID AFTER THE HOLD ENDED, ROOMS NO LONGER FREE: REFUND OR REBOOK"
GUEST_TEXT = ("We received your payment, but the rooms were released. Our team will contact you shortly about it.")


def _booking(bot: Any, booking_id: str, link_id: str) -> Any:
    return (bot.repo.get_booking(booking_id) if booking_id else None) or (
        bot.repo.find_booking_by_link(link_id) if link_id else None)


def rooms_gone(bot: Any, b: Any) -> bool:
    """Every part of the stay is checked: its hold ended (expired / cancelled) and its rooms are no longer free."""
    now = bot.clock.now()
    for x in bot.repo.list_group(b) or [b]:
        rt = bot.repo.get_room_type(x.room_type_id)
        if x.status in ("expired", "cancelled") and rt is not None \
                and bot.repo.free_rooms(rt, x.check_in, x.check_out, now) < x.rooms:
            return True
    return False


def mark_paid(bot: Any, *, booking_id: str = "", link_id: str = "", payment_id: str = "", out: Any = None) -> str:
    """bot.mark_paid, except a late payment whose rooms are gone: paid | already_paid | late_rooms_gone | unknown."""
    b = _booking(bot, booking_id, link_id)
    if b is None:
        return "unknown"
    if b.status in ("expired", "cancelled") and rooms_gone(bot, b):
        return _late(bot, b, payment_id, out)
    outcome = bot.mark_paid(booking_id=b.id, link_id=link_id, payment_id=payment_id, out=out)
    return "late_rooms_gone" if outcome == "refund_needed" else outcome


def _late(bot: Any, b: Any, payment_id: str, out: Any) -> str:
    fresh = bot.repo.get_booking(b.id) or b
    if LATE_MARK in (fresh.notes or ""):
        return "late_rooms_gone"  # the webhook and our own check both saw it: told once
    fresh.notes = (f"{fresh.notes or ''} {LATE_MARK}").strip()
    fresh.razorpay_payment_id = payment_id or fresh.razorpay_payment_id
    bot.repo.save_booking(fresh)
    guest = fresh.guest_name or "guest"
    alert = (f"Late payment for {guest} ({fresh.guest_phone}), rooms no longer free — refund or rebook. "
             f"Booking {fresh.id}, Rs {fresh.advance}.")
    if out is not None:
        out.staff("staff:late_payment", alert, bot.staff_numbers(fresh.property_id))
        out.text("late_payment", GUEST_TEXT)
        return "late_rooms_gone"
    bot.alert_staff(alert, fresh.property_id)
    from app.resort.agent.outbox import _send_exact
    if not _send_exact(bot, fresh.guest_phone, GUEST_TEXT):
        log.warning("Late payment: the guest %s could not be told", fresh.guest_phone[-4:])
    return "late_rooms_gone"
