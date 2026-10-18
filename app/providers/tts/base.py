"""Text-to-speech interface."""

from __future__ import annotations

from typing import Protocol


class TTSError(RuntimeError):
    pass


class TTSProvider(Protocol):
    model: str
    speaker: str

    def synthesize(self, text: str, language_code: str) -> bytes:
        """WAV audio for `text`. Raises TTSError on failure."""
        ...
