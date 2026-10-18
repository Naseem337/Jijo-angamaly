"""Sarvam text-to-speech (POST https://api.sarvam.ai/text-to-speech).

Checked against the live API and docs on 2026-10-18/19:
  - Only bulbul:v3 speaks Malayalam (v4-flash doesn't offer it yet); input must be in
    native script, since romanised Malayalam "significantly reduces output quality".
  - Request: text, language_code, model, speaker, pace, speech_sample_rate; max 2500 chars.
  - Response: {"audios": [<base64 WAV>]}. `opus` output is rejected on v3, so the WAV is
    converted to OGG/Opus with ffmpeg (app.providers.audio.wav_to_ogg_opus).
  - A ~15 s Malayalam sentence took 5–6 s to generate.
"""

from __future__ import annotations

import base64
import binascii

import httpx

from app.providers.tts.base import TTSError

URL = "https://api.sarvam.ai/text-to-speech"
MAX_CHARS = 2500


class SarvamTTS:
    def __init__(self, api_key: str, *, model: str = "bulbul:v3", speaker: str = "kavya",
                 pace: float = 1.0, sample_rate: int = 24000, timeout_s: float = 20.0,
                 http: httpx.Client | None = None) -> None:
        self._headers = {"api-subscription-key": api_key}
        self.model = model
        self.speaker = speaker
        self.pace = pace
        self.sample_rate = sample_rate
        self._http = http or httpx.Client(timeout=timeout_s)

    def synthesize(self, text: str, language_code: str) -> bytes:
        text = text.strip()
        if not text:
            raise TTSError("nothing to say")
        if len(text) > MAX_CHARS:
            raise TTSError(f"text too long for TTS ({len(text)} > {MAX_CHARS} chars)")
        body = {"text": text, "language_code": language_code, "model": self.model,
                "speaker": self.speaker, "pace": self.pace, "speech_sample_rate": self.sample_rate}
        try:
            resp = self._http.post(URL, headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            raise TTSError(f"Sarvam TTS request failed: {exc!r}") from exc
        if resp.status_code != 200:
            try:
                message = resp.json().get("error", {}).get("message", "")
            except ValueError:
                message = resp.text[:300]
            raise TTSError(f"Sarvam TTS HTTP {resp.status_code}: {message}")
        try:
            return base64.b64decode(resp.json()["audios"][0], validate=True)
        except (KeyError, IndexError, ValueError, binascii.Error) as exc:
            raise TTSError(f"unexpected Sarvam TTS response: {resp.text[:200]}") from exc
