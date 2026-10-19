"""Gemini text-to-speech (generateContent with audio output).

Chosen by the user on 2026-10-19 after comparing samples: voice "Algenib" with a style
instruction (a calm, warm Malayali salesman in his late 40s) sounded the most human.
Gemini follows the style instruction, which Sarvam can't.

  - POST .../v1beta/models/<model>:generateContent, key in the x-goog-api-key header.
  - generationConfig.responseModalities = ["AUDIO"], speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName.
  - The audio comes back base64 in candidates[0].content.parts[0].inlineData. It was a
    complete WAV file when tested; raw 16-bit PCM (audio/L16;rate=…) is wrapped as WAV.
  - A ~18 s Malayalam line took 6–11 s. The free tier has small rate limits, which is
    why VoiceOut falls back to the backup voice.
  - How to give it a style (tested 2026-10-19/20, each result transcribed with Sarvam STT):
      "<style> Say: <text>"        -> it READ THE INSTRUCTION ALOUD (22 s note).
      systemInstruction            -> HTTP 400 "Developer instruction is not enabled for this model".
      director's-notes format below -> only the transcript is spoken (8 s), and the user approved it.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import wave

import httpx

from app.providers.tts.base import TTSError

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_CHARS = 2500

DEFAULT_PROFILE = "an experienced Malayali car salesman in his late 40s"
DEFAULT_STYLE = "calm, warm, slightly deep and mature, relaxed and casual, natural pauses."


def _as_wav(data: bytes, mime_type: str) -> bytes:
    if data[:4] == b"RIFF":
        return data
    rate = int(m.group(1)) if (m := re.search(r"rate=(\d+)", mime_type)) else 24000
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    return out.getvalue()


class GeminiTTS:
    def __init__(self, api_key: str, *, model: str, speaker: str = "Algenib", style: str = DEFAULT_STYLE,
                 profile: str = DEFAULT_PROFILE, timeout_s: float = 30.0, http: httpx.Client | None = None) -> None:
        self._headers = {"x-goog-api-key": api_key}
        self.model = model
        self.speaker = speaker
        self.style = style.strip()
        self.profile = profile.strip()
        self._http = http or httpx.Client(timeout=timeout_s)

    def synthesize(self, text: str, language_code: str) -> bytes:
        text = text.strip()
        if not text:
            raise TTSError("nothing to say")
        if len(text) > MAX_CHARS:
            raise TTSError(f"text too long for TTS ({len(text)} > {MAX_CHARS} chars)")
        body = {
            # Exactly the tested layout: the model treats the notes as direction, not script.
            "contents": [{"parts": [{"text": (f"# AUDIO PROFILE: {self.profile}\n"
                                              f"### DIRECTOR'S NOTES\nStyle: {self.style}\n"
                                              f"#### TRANSCRIPT\n{text}")}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.speaker}}},
            },
        }
        try:
            resp = self._http.post(f"{BASE}/{self.model}:generateContent", headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            raise TTSError(f"Gemini TTS request failed: {exc!r}") from exc
        if resp.status_code != 200:
            try:
                message = resp.json().get("error", {}).get("message", "")
            except ValueError:
                message = resp.text
            raise TTSError(f"Gemini TTS HTTP {resp.status_code}: {message[:200]}")
        try:
            inline = resp.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
            return _as_wav(base64.b64decode(inline["data"]), inline.get("mimeType", ""))
        except (KeyError, IndexError, ValueError, binascii.Error) as exc:
            raise TTSError(f"unexpected Gemini TTS response: {resp.text[:200]}") from exc
