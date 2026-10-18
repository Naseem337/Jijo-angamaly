"""Sending through the WhatsApp Cloud API (Graph API). Media upload/download come in step 7/8."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.phone import mask_phone, to_wa_id

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 4096  # WhatsApp's limit for a text message body
MAX_MEDIA_BYTES = 16 * 1024 * 1024  # WhatsApp's own limit for audio
MEDIA_TIMEOUT_S = 60.0


class WhatsAppError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code  # Graph error code, e.g. 131047 = outside the 24 h window


class WhatsAppClient:
    def __init__(self, phone_number_id: str, access_token: str, *, api_version: str = "v25.0",
                 timeout_s: float = 15.0, http: httpx.Client | None = None) -> None:
        self._graph = f"https://graph.facebook.com/{api_version}"
        self._url = f"{self._graph}/{phone_number_id}/messages"
        self._media_url = f"{self._graph}/{phone_number_id}/media"
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

    def upload_media(self, data: bytes, mime_type: str = "audio/ogg", filename: str = "voice.ogg") -> str:
        """Upload a file for sending; returns its media id (WhatsApp keeps it ~30 days)."""
        try:
            resp = self._http.post(self._media_url, headers=self._headers,
                                   files={"file": (filename, data, mime_type)},
                                   data={"messaging_product": "whatsapp", "type": mime_type},
                                   timeout=MEDIA_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"media upload failed: {exc!r}") from exc
        if resp.status_code != 200 or "id" not in resp.text:
            raise WhatsAppError(f"media upload HTTP {resp.status_code}: {resp.text[:300]}", status=resp.status_code)
        return resp.json()["id"]

    def send_audio(self, to: str, media_id: str, *, voice: bool = True) -> str:
        """Send uploaded audio; voice=True shows it as a voice note (needs mono OGG/Opus)."""
        data = self._post({"recipient_type": "individual", "to": to_wa_id(to), "type": "audio",
                           "audio": {"id": media_id, "voice": voice}})
        message_id = data["messages"][0]["id"]
        log.info("WhatsApp voice note sent to %s (%s)", mask_phone(to), message_id)
        return message_id

    def send_template(self, to: str, name: str, language: str, body_params: list[str]) -> str:
        """Send an approved template (alerts, staff notices). Used from step 9/10."""
        components = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]}]
        data = self._post({"to": to_wa_id(to), "type": "template",
                           "template": {"name": name, "language": {"code": language},
                                        "components": components}})
        return data["messages"][0]["id"]

    def download_media(self, media_id: str, *, max_bytes: int = MAX_MEDIA_BYTES) -> tuple[bytes, str]:
        """(content, mime type) of an inbound voice note / image. Two calls: the media id
        gives a short-lived URL, which is fetched with the same token. Kept in memory only."""
        try:
            meta = self._get_with_retry(f"{self._graph}/{media_id}")
            if meta.status_code != 200:
                raise WhatsAppError(f"media lookup HTTP {meta.status_code}: {meta.text[:200]}",
                                    status=meta.status_code)
            info = meta.json()
            if int(info.get("file_size") or 0) > max_bytes:
                raise WhatsAppError(f"media too large ({info.get('file_size')} bytes)")
            resp = self._get_with_retry(info["url"])
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"media download failed: {exc!r}") from exc
        except (KeyError, ValueError) as exc:
            raise WhatsAppError(f"unexpected media lookup response: {exc!r}") from exc
        if resp.status_code != 200:
            raise WhatsAppError(f"media download HTTP {resp.status_code}", status=resp.status_code)
        if len(resp.content) > max_bytes:
            raise WhatsAppError(f"media too large ({len(resp.content)} bytes)")
        return resp.content, info.get("mime_type") or resp.headers.get("content-type", "")

    def _get_with_retry(self, url: str) -> httpx.Response:
        """Media fetches: Meta's CDN is sometimes slow (a 15 s timeout failed live on
        2026-10-18), so allow MEDIA_TIMEOUT_S and try a second time on a timeout/connection error."""
        try:
            return self._http.get(url, headers=self._headers, timeout=MEDIA_TIMEOUT_S)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            log.warning("Media fetch failed (%r), retrying once", exc)
            return self._http.get(url, headers=self._headers, timeout=MEDIA_TIMEOUT_S)

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
