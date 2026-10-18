"""Text-to-speech providers. build_tts() picks one from a client's config."""

from __future__ import annotations

from app.core.client_config import ClientConfig
from app.providers.tts.base import TTSError, TTSProvider
from app.providers.tts.mock import MockTTS
from app.providers.tts.sarvam import SarvamTTS
from app.settings import SecretResolver

__all__ = ["MockTTS", "SarvamTTS", "TTSError", "TTSProvider", "build_tts"]


def build_tts(config: ClientConfig, secrets: SecretResolver) -> TTSProvider:
    """Raises SecretNotFound if the API key is missing."""
    cfg = config.providers.tts
    if cfg.kind == "mock":
        return MockTTS()
    # voice.tts_timeout_s: the voice note is a bonus after the text, so don't wait long for it.
    return SarvamTTS(secrets.get(cfg.api_key_secret or ""), model=cfg.model or "bulbul:v3",
                     speaker=cfg.speaker or "kavya", pace=cfg.pace, timeout_s=config.voice.tts_timeout_s)
