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
