"""Text-to-speech providers. build_tts() picks one from a client's config."""

from __future__ import annotations

import logging

from app.core.client_config import ClientConfig, SpeechProviderConfig
from app.providers.tts.base import TTSError, TTSProvider
from app.providers.tts.gemini import DEFAULT_PROFILE, DEFAULT_STYLE, GeminiTTS
from app.providers.tts.mock import MockTTS
from app.providers.tts.sarvam import SarvamTTS
from app.settings import SecretNotFound, SecretResolver

__all__ = ["GeminiTTS", "MockTTS", "SarvamTTS", "TTSError", "TTSProvider", "build_tts"]

log = logging.getLogger(__name__)


def _build_one(cfg: SpeechProviderConfig, config: ClientConfig, secrets: SecretResolver) -> TTSProvider:
    if cfg.kind == "mock":
        return MockTTS()
    key = secrets.get(cfg.api_key_secret or "")
    # voice.tts_timeout_s: the voice note is a bonus after the text, so don't wait long for it.
    timeout = config.voice.tts_timeout_s
    if cfg.kind == "gemini":
        if not cfg.model:
            raise SecretNotFound("gemini TTS has no model name (GEMINI_TTS_MODEL)")
        return GeminiTTS(key, model=cfg.model, speaker=cfg.speaker or "Algenib",
                         style=cfg.style or DEFAULT_STYLE,
                         profile=f"{config.bot_name}, {DEFAULT_PROFILE}", timeout_s=timeout)
    return SarvamTTS(key, model=cfg.model or "bulbul:v3", speaker=cfg.speaker or "anand", pace=cfg.pace,
                     temperature=cfg.temperature, timeout_s=timeout)


def build_tts(config: ClientConfig, secrets: SecretResolver) -> list[TTSProvider]:
    """Main voice first, then the backup. Providers without a key are skipped; raises
    SecretNotFound only if none is usable."""
    voices: list[TTSProvider] = []
    errors: list[str] = []
    for cfg in (config.providers.tts, config.providers.tts_backup):
        if cfg is None:
            continue
        try:
            voices.append(_build_one(cfg, config, secrets))
        except SecretNotFound as exc:
            errors.append(str(exc))
            log.warning("TTS voice %s skipped: %s", cfg.kind, exc)
    if not voices:
        raise SecretNotFound("; ".join(errors) or "no TTS configured")
    return voices
