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
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.resort.i18n import t
from app.resort.payments import PaymentEvent, parse_event, verify_webhook_signature
from app.resort.services import ResortServices, build_resort_services, get_services
from app.whatsapp.parse import parse_echoes, parse_webhook, verify_signature

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
async def receive(request: Request) -> dict[str, int]:
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
        _WORKERS.submit(_handle_message, services, msg)
    echoes = parse_echoes(payload, os.environ.get("RESORT_WHATSAPP_PHONE_NUMBER_ID", ""))
    for echo in echoes:  # staff typed in the phone app: pause the bot for that guest
        _WORKERS.submit(_handle_echo, services, echo)
    return {"received": len(messages)}


# Guest messages run on their own threads (stress test, 2026-11-18). FastAPI's BackgroundTasks share one pool of 40
# threads with every plain `def` route, so 40 guests waiting on the AI (each turn holds a thread for its 2.5-8 s
# "wait for a second message" plus the AI calls) froze /health (Railway's restart check) and the dashboard for
# seconds. RESORT_BOT_THREADS caps how many guests are answered at the same moment; the rest queue in order.
class _Workers:
    def __init__(self, threads: int) -> None:
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="resort-msg")
        self._pending: set[Future] = set()
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future:
        future = self._pool.submit(fn, *args)
        with self._lock:
            self._pending.add(future)
        future.add_done_callback(self._done)
        return future

    def _done(self, future: Future) -> None:
        with self._lock:
            self._pending.discard(future)

    def wait_idle(self, timeout: float = 10.0) -> None:
        """Tests: block until every guest message handed over so far has been handled."""
        with self._lock:
            pending = list(self._pending)
        wait(pending, timeout=timeout)


_WORKERS = _Workers(int(os.environ.get("RESORT_BOT_THREADS") or 64))
wait_idle = _WORKERS.wait_idle


def _handle_echo(services: ResortServices, echo) -> None:
    try:
        log.info("Resort staff echo %s: %s", echo.id, services.bot.human_replied(echo))
    except Exception:
        log.exception("Failed to handle resort echo %s", echo.id)


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
        if bot.config.agent_mode == "agent":  # a payment after the 9-minute hold: rooms re-checked (agent/late_pay.py)
            from app.resort.agent import late_pay
            return late_pay.mark_paid(bot, **ids, payment_id=event.payment_id)
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
    """Expire unpaid holds and message those guests; remind holds about to expire.

    AGENT_MODE=agent (2026-11-26): the old flow's "Your payment link is still open for N more minutes" reminder is
    skipped; payment confirmation, hold release and the ID-proof jobs keep running (flow.py is not changed)."""
    bot = (services or get_services()).bot
    if bot.config.agent_mode != "agent":
        return bot.sweep(now)
    return agent_sweep(bot, now)


def agent_sweep(bot, now=None) -> dict[str, int]:
    """BookingBot.sweep() for the agent: payments found, ONE reminder before the hold ends, the Razorpay link cancelled
    when our hold ends (9 min; the link itself lives at least 15), then the rooms released."""
    now = now or bot.clock.now()
    found = bot.check_payments()
    reminded = remind_once(bot, now)
    closed = close_due_links(bot, now)
    expired = bot.repo.expire_holds(now)  # released quietly: no "expired" message to the guest
    out = {"expired": len(expired), "reminded": reminded}
    if closed:
        out["links_closed"] = closed
    try:  # owner, 2026-11-26: one open hold per guest (all but each guest's newest stay are cancelled as replaced)
        from app.resort.agent import holds
        if cleaned := holds.cleanup_all(bot):
            out["duplicate_holds_closed"] = sum(cleaned.values())
            log.info("Resort sweep: duplicate holds closed %s", cleaned)
    except Exception:
        log.exception("Duplicate hold cleanup failed")
    if found:
        out["paid_found"] = found
    try:
        if reminders := bot.remind_ids(now):
            out["id_reminders"] = reminders
        if purged := bot._purge_id_proofs(now):
            out["id_purged"] = purged
    except Exception:
        log.exception("ID reminder / purge failed")
    return out


REMIND_BEFORE = timedelta(minutes=3)  # owner, 2026-11-27: one reminder before the 9-minute hold ends


def _link_holds(bot, now) -> list:
    """Unpaid holds that carry a payment link (the first part of a stay over two room types holds the link)."""
    return [b for b in bot.repo.list_bookings(status="hold", limit=500)
            if b.payment_link_id and not (b.group_id and b.group_id != b.id) and b.hold_expires_at]


def close_due_links(bot, now) -> int:
    """Our hold ended (9 min) but Razorpay's link lives at least 15 min: the link is cancelled through the Razorpay API
    before the rooms are released, so it can no longer be paid."""
    closed = 0
    for b in _link_holds(bot, now):
        if b.hold_expires_at > now:
            continue
        try:
            bot.payments.cancel_link(b.payment_link_id)
            closed += 1
        except Exception as exc:  # the rooms are still released; a late payment is re-checked (agent/late_pay.py)
            log.warning("Could not cancel the payment link of %s: %s", b.id, exc)
    return closed


def remind_once(bot, now) -> int:
    """ONE short reminder per link, REMIND_BEFORE the hold ends; never after STOP, never for a replaced enquiry."""
    sent = 0
    for b in _link_holds(bot, now):
        left = b.hold_expires_at - now
        if "[reminded]" in (b.notes or "") or not (timedelta(0) < left <= REMIND_BEFORE):
            continue
        if bot.st_of(b.guest_phone).get("opted_out"):
            continue
        if any(o.created_at > b.created_at and o.id != b.id and o.group_id != b.id and o.status == "hold"
               for o in bot.repo.list_bookings(phone=b.guest_phone, limit=20)):
            continue  # a newer stay of this guest replaces this one
        b.notes = (b.notes + " [reminded]").strip()
        bot.repo.save_booking(b)
        mins = max(1, int(left.total_seconds() // 60))
        bot._notify_guest(b, t("pending", b.language, id=b.id, minutes=mins, url=b.payment_link_url),
                          "pending", bot._params(b))
        sent += 1
    return sent


def start_sweeper(services: ResortServices, every_seconds: int = 15) -> Callable[[], None]:
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
