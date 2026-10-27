"""WhatsApp Cloud API webhook payloads → InboundMessage, plus the signature check."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.phone import try_normalize_phone

TEXT_LIKE = ("text", "button", "interactive")


@dataclass(frozen=True)
class InboundMessage:
    id: str  # wamid…, used for dedupe and read receipts
    phone: str  # sender, E.164
    type: str  # text | button | interactive | audio | image | sticker | location | …
    timestamp: datetime
    text: str = ""  # message text, or the tapped button's title
    media_id: str | None = None  # audio / image / document
    profile_name: str | None = None
    context_id: str | None = None  # the message this one replies to (e.g. an alert)
    reply_id: str | None = None  # id of the tapped list row / reply button (interactive messages)

    @property
    def is_text(self) -> bool:
        return self.type in TEXT_LIKE and bool(self.text.strip())


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Meta signs every POST: X-Hub-Signature-256: sha256=<hex HMAC of the raw body>."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _text_of(msg: dict[str, Any]) -> str:
    kind = msg.get("type")
    if kind == "text":
        return (msg.get("text") or {}).get("body", "")
    if kind == "button":  # quick-reply button on a template
        return (msg.get("button") or {}).get("text", "")
    if kind == "interactive":
        inner = msg.get("interactive") or {}
        reply = inner.get("button_reply") or inner.get("list_reply") or {}
        return reply.get("title", "")
    return ""


def _reply_id_of(msg: dict[str, Any]) -> str | None:
    if msg.get("type") != "interactive":
        return None
    inner = msg.get("interactive") or {}
    reply = inner.get("button_reply") or inner.get("list_reply") or {}
    return reply.get("id")


def parse_webhook(payload: dict[str, Any], phone_number_id: str) -> list[InboundMessage]:
    """Customer messages sent to this client's number. Delivery/read statuses, other
    numbers' events and senders we can't parse are skipped."""
    found: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if change.get("field") != "messages":
                continue
            if (value.get("metadata") or {}).get("phone_number_id") != phone_number_id:
                continue
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
            for msg in value.get("messages") or []:
                phone = try_normalize_phone("+" + str(msg.get("from", "")))
                if not phone or not msg.get("id"):
                    continue
                kind = msg.get("type", "unknown")
                media = msg.get(kind) if isinstance(msg.get(kind), dict) else {}
                found.append(InboundMessage(
                    id=msg["id"],
                    phone=phone,
                    type=kind,
                    timestamp=datetime.fromtimestamp(int(msg.get("timestamp", 0)), tz=timezone.utc),
                    text=_text_of(msg),
                    media_id=media.get("id"),
                    profile_name=names.get(msg.get("from")),
                    context_id=(msg.get("context") or {}).get("id"),
                    reply_id=_reply_id_of(msg),
                ))
    return found
