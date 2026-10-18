"""Replying with a voice note (PLAN.md step 8).

The text reply is always sent first; the voice note follows when the client's voice
policy asks for it ("mirror": only to customers who sent a voice note). Anything that
goes wrong here only costs the voice note, never the reply.

Cost controls:
  - a daily character cap per client (business rule `voice.daily_char_cap`), counted
    atomically in the repository before calling TTS;
  - a cache of uploaded voice notes keyed by (model, speaker, language, text): repeated
    phrases such as "please say that again" are synthesised and uploaded once and then
    re-sent by media id for 25 days (WhatsApp keeps uploads about 30 days).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from app.core.client_config import ClientConfig
from app.core.clock import Clock, ist_today
from app.core.phone import mask_phone
from app.providers.audio import AudioError, wav_to_ogg_opus
from app.providers.tts import TTSError, TTSProvider
from app.storage.models import Message
from app.storage.repository import Repository
from app.whatsapp.client import WhatsAppClient, WhatsAppError

log = logging.getLogger(__name__)

CACHE_DAYS = 25
CHAR_COUNTER = "tts_chars"


class VoiceOut:
    def __init__(self, config: ClientConfig, tts: TTSProvider | list[TTSProvider], whatsapp: WhatsAppClient,
                 repo: Repository, clock: Clock, ffmpeg: str) -> None:
        self.config = config
        self.voices = tts if isinstance(tts, list) else [tts]  # main voice, then backups
        self.whatsapp = whatsapp
        self.repo = repo
        self.clock = clock
        self.ffmpeg = ffmpeg

    def wants_voice(self, source: str) -> bool:
        policy = self.config.voice.policy
        return policy == "always" or (policy == "mirror" and source == "voice")

    @staticmethod
    def _cache_key(tts: TTSProvider, text: str, language_code: str) -> str:
        raw = f"{tts.model}|{tts.speaker}|{getattr(tts, 'style', '')}|{language_code}|{text}"
        return "tts:" + hashlib.sha256(raw.encode()).hexdigest()

    def send(self, phone: str, spoken_text: str, language: str) -> bool:
        """Speak `spoken_text` to the customer. Returns False (and logs why) on any failure."""
        cid, now = self.config.client_id, self.clock.now()
        language_code = "en-IN" if language == "en" else "ml-IN"
        try:
            media_id = self._voice_note(spoken_text, language_code)
            if media_id is None:
                return False
            self.whatsapp.send_audio(phone, media_id, voice=True)
        except (AudioError, WhatsAppError) as exc:
            log.error("Voice reply to %s failed, text was sent: %s", mask_phone(phone), exc)
            return False
        self.repo.add_message(Message(cid, phone, "out", spoken_text, self.clock.now(), source="voice",
                                      meta={"voice_note": True}))
        return True

    def _voice_note(self, text: str, language_code: str) -> str | None:
        """Media id of the voice note: from the cache, or made by the first voice that works."""
        cid, now = self.config.client_id, self.clock.now()
        for tts in self.voices:  # a cached note from any voice is fine to reuse
            if media_id := self.repo.get_cached_media(cid, self._cache_key(tts, text, language_code), now):
                return media_id
        day = ist_today(self.clock).isoformat()
        if not self.repo.increment_counter(cid, CHAR_COUNTER, day, by=len(text), cap=self.config.voice.daily_char_cap):
            log.warning("Daily TTS character cap reached for %s; sending text only", cid)
            return None
        for tts in self.voices:
            try:
                wav = tts.synthesize(text, language_code)
            except TTSError as exc:
                log.warning("Voice %s/%s failed (%s); trying the next voice", tts.model, tts.speaker, exc)
                continue
            media_id = self.whatsapp.upload_media(wav_to_ogg_opus(wav, self.ffmpeg), "audio/ogg")
            self.repo.put_cached_media(cid, self._cache_key(tts, text, language_code), media_id,
                                       now + timedelta(days=CACHE_DAYS))
            return media_id
        log.error("No voice could speak the reply; text only")
        return None
