"""Voice note → text: download, length check, speech-to-text, "did we hear it?" check.

The audio only ever lives in memory (and briefly in a temp folder while ffmpeg splits a
long note); it is never stored. Only the transcript is kept, as the customer's message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from app.core.language import classify_confirmation
from app.core.money import extract_numbers
from app.providers.audio import ogg_duration_seconds
from app.providers.stt import STTError, STTProvider, Transcript
from app.whatsapp.client import WhatsAppClient, WhatsAppError

log = logging.getLogger(__name__)

Problem = Literal["too_long", "unclear", "failed"]

MIN_LANGUAGE_PROBABILITY = 0.4

VOICE_PROBLEM_REPLY: dict[Problem, dict[str, str]] = {
    "too_long": {
        "ml": "വോയ്‌സ് കുറച്ച് നീളം കൂടുതലാ, ഒരു മിനിറ്റിൽ താഴെ അയക്കാമോ?",
        "manglish": "Voice kurachu neelam koodipoyi, oru minute-il thazhe ayakkamo?",
        "en": "That voice note is a bit long, can you send one under a minute?",
    },
    "unclear": {
        "ml": "മനസ്സിലായില്ല, ഒന്നൂടെ പറയാമോ?",
        "manglish": "Manasilayilla, onnu koodi parayamo?",
        "en": "Didn't catch that, can you say it again?",
    },
    "failed": {
        "ml": "ഇപ്പോൾ വോയ്‌സ് കേൾക്കാൻ പറ്റുന്നില്ല, ടൈപ്പ് ചെയ്യാമോ?",
        "manglish": "Ippo voice kelkkan pattunnilla, onnu type cheyyamo?",
        "en": "Can't play voice notes right now, can you type it?",
    },
}


@dataclass(frozen=True)
class VoiceResult:
    text: str | None = None
    problem: Problem | None = None
    duration_s: float | None = None
    detail: str = ""


def is_unclear(t: Transcript) -> bool:
    """Too little or too doubtful to act on (PLAN.md Q6).

    One-word notes are fine when they are a yes/no ("sheri", "illa") or a number,
    because those answer a read-back; otherwise one word needs a confident language match.
    """
    text = t.text.strip()
    if not text:
        return True
    if t.language_probability is not None and t.language_probability < MIN_LANGUAGE_PROBABILITY:
        return True
    if len(text.split()) >= 2:
        return False
    if classify_confirmation(text) or extract_numbers(text):
        return False
    return (t.language_probability or 0) < 0.8


class VoiceIn:
    def __init__(self, whatsapp: WhatsAppClient, stt: STTProvider, *, max_seconds: int = 60) -> None:
        self.whatsapp = whatsapp
        self.stt = stt
        self.max_seconds = max_seconds

    def process(self, media_id: str | None) -> VoiceResult:
        if not media_id:
            return VoiceResult(problem="failed", detail="no media id")
        try:
            audio, mime = self.whatsapp.download_media(media_id)
        except WhatsAppError as exc:
            log.error("Voice download failed: %s", exc)
            return VoiceResult(problem="failed", detail=str(exc))

        duration = ogg_duration_seconds(audio)
        if duration is not None and duration > self.max_seconds:
            return VoiceResult(problem="too_long", duration_s=duration)
        try:
            transcript = self.stt.transcribe(audio, mime)
        except STTError as exc:
            log.error("Speech-to-text failed: %s", exc)
            return VoiceResult(problem="too_long" if exc.too_long else "failed", duration_s=duration, detail=str(exc))
        finally:
            del audio  # nothing keeps the recording

        if is_unclear(transcript):
            return VoiceResult(problem="unclear", duration_s=duration,
                               detail=f"{transcript.text!r} p={transcript.language_probability}")
        return VoiceResult(text=transcript.text.strip(), duration_s=duration)
