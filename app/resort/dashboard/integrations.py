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


def send_staff_text(phone: str, text: str) -> str | None:
    """Send a free-text WhatsApp message as staff. Returns an error text, or None on success."""
    try:
        from app.resort import web  # type: ignore[attr-defined]
        fn = getattr(web, "send_text", None)
        if fn is None:
            return "WhatsApp is not connected yet"
        fn(phone, text)
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
