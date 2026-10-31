"""Resort webhooks: Meta (WhatsApp) at /resort/webhook and Razorpay at /resort/razorpay/webhook.

Meta env: RESORT_WHATSAPP_VERIFY_TOKEN, RESORT_WHATSAPP_APP_SECRET, RESORT_WHATSAPP_PHONE_NUMBER_ID.
Razorpay env: RAZORPAY_WEBHOOK_SECRET. Both POSTs answer 200 quickly and work in a background task.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.resort.payments import PaymentEvent, parse_event, verify_webhook_signature
from app.resort.services import ResortServices, build_resort_services, get_services
from app.whatsapp.parse import parse_webhook, verify_signature

log = logging.getLogger(__name__)
router = APIRouter()

__all__ = ["router", "build_resort_services", "get_services", "sweep", "start_sweeper", "handle_payment_event"]


def _services(request: Request) -> ResortServices:
    return getattr(request.app.state, "resort", None) or get_services()


@router.get("/resort/webhook", response_class=PlainTextResponse)
def verify(request: Request) -> str:
    params = request.query_params
    expected = os.environ.get("RESORT_WHATSAPP_VERIFY_TOKEN", "")
    if expected and params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == expected:
        return params.get("hub.challenge", "")
    raise HTTPException(403, "verification failed")


@router.post("/resort/webhook")
async def receive(request: Request, background: BackgroundTasks) -> dict[str, int]:
    body = await request.body()
    secret = os.environ.get("RESORT_WHATSAPP_APP_SECRET", "")
    if not secret or not verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
        log.warning("Rejected resort webhook POST: bad signature")
        raise HTTPException(403, "bad signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "invalid JSON") from exc
    services = _services(request)
    messages = parse_webhook(payload, os.environ.get("RESORT_WHATSAPP_PHONE_NUMBER_ID", ""))
    for msg in messages:
        background.add_task(_handle_message, services, msg)
    return {"received": len(messages)}


def _handle_message(services: ResortServices, msg) -> None:
    try:
        outcome = services.bot.handle(msg)
        log.info("Resort message %s: %s", msg.id, outcome)
    except Exception:  # a crash must not lose the log line or take the server down
        log.exception("Failed to handle resort message %s", msg.id)


# --------------------------------------------------------------------------- Razorpay


def handle_payment_event(services: ResortServices, event: PaymentEvent) -> str:
    bot = services.bot
    ids = {"booking_id": event.booking_id, "link_id": event.payment_link_id}
    if event.kind == "paid":
        return bot.mark_paid(**ids, payment_id=event.payment_id)
    if event.kind == "failed":
        return bot.payment_failed(**ids, payment_id=event.payment_id)
    return bot.payment_expired(**ids)  # expired / cancelled


@router.post("/resort/razorpay/webhook")
async def razorpay_webhook(request: Request, background: BackgroundTasks) -> dict[str, str]:
    body = await request.body()
    if not verify_webhook_signature(body, request.headers.get("x-razorpay-signature"),
                                    os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")):
        log.warning("Rejected Razorpay webhook: bad signature")
        raise HTTPException(400, "bad signature")
    try:
        event = parse_event(json.loads(body))
    except ValueError as exc:
        raise HTTPException(400, "invalid JSON") from exc
    if event is None:
        return {"status": "ignored"}
    services = _services(request)

    def run() -> None:
        try:
            log.info("Razorpay %s for %s: %s", event.kind, event.booking_id or event.payment_link_id,
                     handle_payment_event(services, event))
        except Exception:
            log.exception("Razorpay event %s failed", event.kind)

    background.add_task(run)
    return {"status": "ok"}


# --------------------------------------------------------------------------- periodic job


def sweep(now=None, services: ResortServices | None = None) -> dict[str, int]:
    """Expire unpaid holds and message those guests; remind holds about to expire."""
    return (services or get_services()).bot.sweep(now)


def start_sweeper(services: ResortServices, every_seconds: int = 60) -> Callable[[], None]:
    """Daemon thread that runs sweep() every `every_seconds`; returns a stop function."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(every_seconds):
            try:
                result = sweep(services=services)
                if result["expired"] or result["reminded"]:
                    log.info("Resort sweep: %s", result)
            except Exception:
                log.exception("Resort sweep crashed")

    threading.Thread(target=loop, name="resort-sweep", daemon=True).start()
    return stop.set
