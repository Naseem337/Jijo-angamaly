"""Speech-to-text providers. build_stt() picks one from a client's config."""

from __future__ import annotations

from functools import partial

from app.core.client_config import ClientConfig
from app.providers.audio import find_ffmpeg, split_to_wav_chunks
from app.providers.stt.base import STTError, STTProvider, Transcript
from app.providers.stt.mock import MockSTT
from app.providers.stt.sarvam import SarvamSTT
from app.settings import SecretResolver

__all__ = ["MockSTT", "STTError", "STTProvider", "SarvamSTT", "Transcript", "build_stt"]


def build_stt(config: ClientConfig, secrets: SecretResolver) -> STTProvider:
    """Raises SecretNotFound if the API key is missing."""
    cfg = config.providers.stt
    if cfg.kind == "mock":
        return MockSTT()
    ffmpeg = find_ffmpeg()
    return SarvamSTT(
        secrets.get(cfg.api_key_secret or ""),
        model=cfg.model or "saaras:v4",
        language_code=cfg.language_code,
        keyterms=cfg.keyterms,
        timeout_s=cfg.timeout_s,
        splitter=partial(split_to_wav_chunks, ffmpeg=ffmpeg) if ffmpeg else None,
    )
