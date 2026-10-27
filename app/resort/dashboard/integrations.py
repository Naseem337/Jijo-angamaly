"""Calls into the bot side (payment links, WhatsApp). Imported lazily so the dashboard runs without them."""

from __future__ import annotations

import logging

from app.resort.models import Booking

log = logging.getLogger(__name__)


def resend_payment_link(booking: Booking) -> str | None:
    """Create/refresh the Razorpay link and send it to the guest. Returns an error text, or None on success."""
    try:
        from app.resort import web  # type: ignore[attr-defined]
        fn = getattr(web, "resend_payment_link", None)
        if fn is None:
            return "Payment links are not set up yet"
        fn(booking.id)
        return None
    except Exception as exc:  # noqa: BLE001 - shown to staff, never crashes the page
        log.warning("resend_payment_link failed: %s", exc)
        return f"Could not send the link: {exc}"


def _whatsapp_ready() -> bool:
    import os
    return bool(os.environ.get("RESORT_WHATSAPP_PHONE_NUMBER_ID") and os.environ.get("RESORT_WHATSAPP_ACCESS_TOKEN"))


def send_staff_text(phone: str, text: str) -> str | None:
    """Send a free-text WhatsApp message as staff. Returns an error text, or None on success."""
    try:
        from app.resort import web  # type: ignore[attr-defined]
        fn = getattr(web, "send_text", None)
        if fn is not None:
            fn(phone, text)
            return None
        if not _whatsapp_ready():
            return "WhatsApp is not connected yet"
        from app.resort.services import get_services
        get_services().whatsapp.send_text(phone, text)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("send_text failed: %s", exc)
        return f"Could not send: {exc}"


def announce_paid(booking: Booking) -> str | None:
    """Confirm a desk-recorded payment to the guest on WhatsApp and alert staff.
    Returns a note for staff if the guest could not be messaged, or None on success."""
    import os
    if not (os.environ.get("RESORT_WHATSAPP_PHONE_NUMBER_ID") and os.environ.get("RESORT_WHATSAPP_ACCESS_TOKEN")):
        return "WhatsApp is not connected, so the guest was not messaged"
    try:
        from app.resort.services import get_services
        get_services().bot.announce_paid(booking)
        return None
    except Exception as exc:  # noqa: BLE001 - the payment is already saved; just tell staff
        log.warning("announce_paid failed: %s", exc)
        return f"Saved, but the guest could not be messaged: {exc}"


def announce_cancelled(booking: Booking, refund_amount: int) -> str | None:
    """Tell the guest their booking is cancelled (and what is refunded). Uses the bot's own translated helper when it
    has one (`announce_cancelled`); otherwise plain English text. Returns a note for staff on failure, else None."""
    if not _whatsapp_ready():
        return "WhatsApp is not connected, so the guest was not messaged"
    try:
        from app.resort.services import get_services
        bot = get_services().bot
        if hasattr(bot, "announce_cancelled"):
            bot.announce_cancelled(booking, refund_amount)
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("bot.announce_cancelled failed: %s", exc)
    text = (f"Your booking {booking.id} ({booking.check_in:%d %b} to {booking.check_out:%d %b}) has been cancelled."
            + (f" A refund of ₹{refund_amount:,} has been arranged." if refund_amount else "")
            + " Please reply here if you need any help.")
    return send_staff_text(booking.guest_phone, text)  # TODO(bot fork): translated announce_cancelled


def announce_checked_out(booking: Booking) -> None:
    """Optional hook: the bot may ask for feedback after check-out. Silent if the bot doesn't have it."""
    if not _whatsapp_ready():
        return
    try:
        from app.resort.services import get_services
        fn = getattr(get_services().bot, "announce_checked_out", None)
        if fn is not None:
            fn(booking)
    except Exception as exc:  # noqa: BLE001
        log.info("announce_checked_out skipped: %s", exc)
