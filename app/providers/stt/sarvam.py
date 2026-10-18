"""Sarvam speech-to-text (POST https://api.sarvam.ai/speech-to-text).

Checked against the live API on 2026-10-18:
  - model saaras:v4 (default); language_code "unknown" auto-detects and then returns
    `language_code` + `language_probability`; `keyterms` (JSON list, ≤50) biases spelling.
  - OGG/Opus is accepted directly, but only with the plain MIME type "audio/ogg"
    (WhatsApp's "audio/ogg; codecs=opus" is rejected).
  - Hard limit of 30 s per request ("Audio duration exceeds the maximum limit of 30
    seconds"), so longer notes are cut into pieces with ffmpeg and the texts joined.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx

from app.providers.audio import AudioError, ogg_duration_seconds
from app.providers.stt.base import STTError, Transcript

log = logging.getLogger(__name__)

URL = "https://api.sarvam.ai/speech-to-text"
MAX_SECONDS = 30
CHUNK_SECONDS = 28  # a little under the limit
_MIME_ALIASES = {"audio/opus": "audio/ogg", "audio/x-wav": "audio/wav"}

Splitter = Callable[[bytes, float], list[bytes]]


class SarvamSTT:
    def __init__(self, api_key: str, *, model: str = "saaras:v4", language_code: str = "unknown",
                 keyterms: list[str] | None = None, timeout_s: float = 20.0,
                 splitter: Splitter | None = None, http: httpx.Client | None = None) -> None:
        self._headers = {"api-subscription-key": api_key}
        self.model = model
        self.language_code = language_code
        self.keyterms = list(keyterms or [])[:50]
        self._split = splitter  # None = no ffmpeg: notes over 30 s can't be transcribed
        self._http = http or httpx.Client(timeout=timeout_s)

    def transcribe(self, audio: bytes, mime_type: str) -> Transcript:
        mime = mime_type.split(";")[0].strip().lower() or "audio/ogg"
        mime = _MIME_ALIASES.get(mime, mime)
        duration = ogg_duration_seconds(audio)
        if duration is not None and duration > MAX_SECONDS - 0.5:
            return self._transcribe_in_pieces(audio)
        try:
            return self._post(audio, mime, "voice" + (".ogg" if mime == "audio/ogg" else ""))
        except STTError as exc:
            if exc.too_long:  # duration unknown up front (not OGG) and too long
                return self._transcribe_in_pieces(audio)
            raise

    def _transcribe_in_pieces(self, audio: bytes) -> Transcript:
        if self._split is None:
            raise STTError("voice note longer than 30 s and ffmpeg is not available", too_long=True)
        try:
            pieces = self._split(audio, CHUNK_SECONDS)
        except AudioError as exc:
            raise STTError(str(exc)) from exc
        parts = [self._post(piece, "audio/wav", f"part{i}.wav") for i, piece in enumerate(pieces)]
        best = max(parts, key=lambda p: p.language_probability or 0)
        text = " ".join(p.text.strip() for p in parts if p.text.strip())
        return Transcript(text, best.language_code, best.language_probability)

    def _post(self, audio: bytes, mime: str, filename: str) -> Transcript:
        data = {"model": self.model, "language_code": self.language_code}
        if self.keyterms:
            data["keyterms"] = json.dumps(self.keyterms, ensure_ascii=False)
        try:
            resp = self._http.post(URL, headers=self._headers, files={"file": (filename, audio, mime)}, data=data)
        except httpx.HTTPError as exc:
            raise STTError(f"Sarvam request failed: {exc!r}") from exc
        if resp.status_code != 200:
            try:
                message = resp.json().get("error", {}).get("message", "")
            except ValueError:
                message = resp.text[:300]
            raise STTError(f"Sarvam HTTP {resp.status_code}: {message}",
                           too_long="exceeds the maximum limit" in message)
        body = resp.json()
        return Transcript(body.get("transcript") or "", body.get("language_code"), body.get("language_probability"))
