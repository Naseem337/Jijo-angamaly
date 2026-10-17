"""Sending through the WhatsApp Cloud API (Graph API). Media upload/download come in step 7/8."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.phone import mask_phone, to_wa_id

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 4096  # WhatsApp's limit for a text message body


class WhatsAppError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code  # Graph error code, e.g. 131047 = outside the 24 h window


class WhatsAppClient:
    def __init__(self, phone_number_id: str, access_token: str, *, api_version: str = "v25.0",
                 timeout_s: float = 15.0, http: httpx.Client | None = None) -> None:
        self._url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._http = http or httpx.Client(timeout=timeout_s)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._http.post(self._url, json={"messaging_product": "whatsapp", **body},
                                   headers=self._headers)
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"request failed: {exc!r}") from exc
        if resp.status_code != 200:
            try:
                error = resp.json().get("error", {})
            except ValueError:
                error = {}
            raise WhatsAppError(f"HTTP {resp.status_code}: {error.get('message') or resp.text[:300]}",
                                status=resp.status_code, code=error.get("code"))
        return resp.json()

    def send_text(self, to: str, text: str) -> str:
        """Send a free-form text (only allowed within 24 h of the customer's last message).
        Returns the WhatsApp message id."""
        if len(text) > MAX_TEXT_CHARS:
            text = text[: MAX_TEXT_CHARS - 1] + "…"
        data = self._post({"recipient_type": "individual", "to": to_wa_id(to), "type": "text",
                           "text": {"preview_url": False, "body": text}})
        message_id = data["messages"][0]["id"]
        log.info("WhatsApp text sent to %s (%s)", mask_phone(to), message_id)
        return message_id

    def send_template(self, to: str, name: str, language: str, body_params: list[str]) -> str:
        """Send an approved template (alerts, staff notices). Used from step 9/10."""
        components = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]}]
        data = self._post({"to": to_wa_id(to), "type": "template",
                           "template": {"name": name, "language": {"code": language},
                                        "components": components}})
        return data["messages"][0]["id"]

    def mark_read(self, message_id: str, *, typing: bool = True) -> bool:
        """Blue ticks, plus a "typing…" bubble while the agent thinks. Best effort only."""
        body: dict[str, Any] = {"status": "read", "message_id": message_id}
        if typing:
            body["typing_indicator"] = {"type": "text"}
        try:
            self._post(body)
            return True
        except WhatsAppError as exc:
            log.warning("mark_read failed: %s", exc)
            return False
