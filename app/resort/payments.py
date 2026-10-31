"""Razorpay payment links over plain httpx (no SDK), a signed-webhook check, and a mock for local tests.

Env: RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET (API), RAZORPAY_WEBHOOK_SECRET (webhook signature),
RESORT_WHATSAPP_NUMBER (optional, the business number: after paying, the guest is sent back to the chat).
Without the API keys `build_payments()` returns MockPayments, which hands out a fake link so the whole
flow can be tried locally; staff can then mark the booking paid from the dashboard.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

API = "https://api.razorpay.com/v1"


class PaymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaymentLink:
    id: str
    url: str


class Payments(Protocol):
    def create_payment_link(self, amount_rupees: int, booking_id: str, name: str, phone: str,
                            description: str, expire_by: int) -> PaymentLink: ...


class RazorpayPayments:
    def __init__(self, key_id: str, key_secret: str, *, http: httpx.Client | None = None,
                 callback_url: str = "") -> None:
        self._auth = (key_id, key_secret)
        self._callback_url = callback_url  # where Razorpay's page sends the guest after paying (a wa.me link)
        self._http = http or httpx.Client(timeout=20)

    def create_payment_link(self, amount_rupees: int, booking_id: str, name: str, phone: str,
                            description: str, expire_by: int) -> PaymentLink:
        """`expire_by` is a unix timestamp; Razorpay needs it at least 15 minutes ahead."""
        body: dict[str, Any] = {
            "amount": int(amount_rupees) * 100,  # paise
            "currency": "INR",
            "accept_partial": False,
            "reference_id": booking_id,
            "description": description[:2000],
            "customer": {"name": name or "Guest", "contact": phone},
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "expire_by": expire_by,
            "notes": {"booking_id": booking_id},
        }
        if self._callback_url:
            body["callback_url"], body["callback_method"] = self._callback_url, "get"
        try:
            resp = self._http.post(f"{API}/payment_links", json=body, auth=self._auth)
        except httpx.HTTPError as exc:
            raise PaymentError(f"Razorpay request failed: {exc!r}") from exc
        if resp.status_code not in (200, 201):
            raise PaymentError(f"Razorpay HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return PaymentLink(data["id"], data["short_url"])

    def link_status(self, link_id: str) -> dict[str, Any]:
        """Ask Razorpay whether this link has been paid, so a missed webhook never leaves a guest unconfirmed."""
        try:
            resp = self._http.get(f"{API}/payment_links/{link_id}", auth=self._auth)
        except httpx.HTTPError as exc:
            raise PaymentError(f"Razorpay request failed: {exc!r}") from exc
        if resp.status_code != 200:
            raise PaymentError(f"Razorpay HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        payments = data.get("payments") or []
        return {"paid": data.get("status") == "paid",
                "payment_id": (payments[0].get("payment_id", "") if payments else "")}


class MockPayments:
    """Local testing: a fake link, no money."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.paid_links: dict[str, str] = {}  # link id -> payment id, for tests

    def link_status(self, link_id: str) -> dict[str, Any]:
        return {"paid": link_id in self.paid_links, "payment_id": self.paid_links.get(link_id, "")}

    def create_payment_link(self, amount_rupees: int, booking_id: str, name: str, phone: str,
                            description: str, expire_by: int) -> PaymentLink:
        link_id = "plink_mock_" + secrets.token_hex(4)
        self.created.append({"id": link_id, "amount": amount_rupees, "booking_id": booking_id,
                             "name": name, "phone": phone})
        return PaymentLink(link_id, f"https://rzp.io/mock/{link_id}")


def build_payments(http: httpx.Client | None = None) -> Payments:
    key_id, key_secret = os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", "")
    if key_id and key_secret:
        number = "".join(ch for ch in os.environ.get("RESORT_WHATSAPP_NUMBER", "") if ch.isdigit())
        return RazorpayPayments(key_id, key_secret, http=http, callback_url=f"https://wa.me/{number}" if number else "")
    log.warning("RAZORPAY_KEY_ID/SECRET not set: using mock payment links (testing only)")
    return MockPayments()


def verify_webhook_signature(body: bytes, header: str | None, secret: str) -> bool:
    """Razorpay signs the raw body: X-Razorpay-Signature = hex HMAC-SHA256(body, webhook secret)."""
    if not header or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


@dataclass(frozen=True)
class PaymentEvent:
    kind: str  # paid | expired | failed | cancelled
    payment_link_id: str
    booking_id: str
    payment_id: str = ""
    amount_rupees: int = 0


def parse_event(payload: dict[str, Any]) -> PaymentEvent | None:
    """payment_link.paid / .expired / .cancelled and payment.failed -> PaymentEvent; others -> None."""
    event = payload.get("event", "")
    body = payload.get("payload") or {}
    link = (body.get("payment_link") or {}).get("entity") or {}
    payment = (body.get("payment") or {}).get("entity") or {}
    notes = link.get("notes") or payment.get("notes") or {}
    booking_id = link.get("reference_id") or (notes.get("booking_id") if isinstance(notes, dict) else "") or ""
    link_id = link.get("id") or payment.get("payment_link_id") or ""
    amount = int(payment.get("amount") or link.get("amount_paid") or 0) // 100
    kinds = {"payment_link.paid": "paid", "payment_link.expired": "expired",
             "payment_link.cancelled": "cancelled", "payment.failed": "failed"}
    if event not in kinds or not (link_id or booking_id):
        return None
    return PaymentEvent(kinds[event], link_id, booking_id, payment.get("id", ""), amount)
