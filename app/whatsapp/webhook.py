"""Meta webhook endpoints: GET /webhook/{client_id} (verification), POST (messages).

The POST answers 200 straight away and handles messages in a background task, because
Meta retries deliveries that aren't acknowledged quickly (retries are deduped anyway).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.runtime import ClientRuntime, Runtime
from app.settings import SecretNotFound
from app.whatsapp.parse import parse_webhook, verify_signature

log = logging.getLogger(__name__)
router = APIRouter()


def _client(request: Request, client_id: str) -> ClientRuntime:
    runtime: Runtime = request.app.state.runtime
    client = runtime.get(client_id)
    if client is None:
        raise HTTPException(404, "unknown client")
    return client


def _secret(client: ClientRuntime, name: str) -> str:
    try:
        return client.secret(name)
    except SecretNotFound as exc:
        log.error("Webhook for %s can't run: %s", client.config.client_id, exc)
        raise HTTPException(500, "server not configured") from exc


@router.get("/webhook/{client_id}", response_class=PlainTextResponse)
def verify(client_id: str, request: Request) -> str:
    client = _client(request, client_id)
    params = request.query_params
    expected = _secret(client, client.config.whatsapp.verify_token_secret)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == expected:
        log.info("Webhook verified for %s", client_id)
        return params.get("hub.challenge", "")
    raise HTTPException(403, "verification failed")


@router.post("/webhook/{client_id}")
async def receive(client_id: str, request: Request, background: BackgroundTasks) -> dict[str, int]:
    client = _client(request, client_id)
    body = await request.body()
    app_secret = _secret(client, client.config.whatsapp.app_secret_secret)
    if not verify_signature(app_secret, body, request.headers.get("x-hub-signature-256")):
        log.warning("Rejected webhook POST for %s: bad signature", client_id)
        raise HTTPException(403, "bad signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "invalid JSON") from exc

    messages = parse_webhook(payload, client.config.whatsapp.phone_number_id)
    for msg in messages:
        background.add_task(_handle, client, msg)
    return {"received": len(messages)}


def _handle(client: ClientRuntime, msg) -> None:
    try:
        outcome = client.router.handle(msg)
        log.info("Message %s from %s: %s", msg.id, client.config.client_id, outcome)
    except Exception:  # a crash must not take down the server or lose the log line
        log.exception("Failed to handle message %s for %s", msg.id, client.config.client_id)
