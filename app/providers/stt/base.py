"""Speech-to-text interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class STTError(RuntimeError):
    def __init__(self, message: str, *, too_long: bool = False) -> None:
        super().__init__(message)
        self.too_long = too_long  # the audio is longer than we can transcribe


@dataclass(frozen=True)
class Transcript:
    text: str
    language_code: str | None = None  # BCP-47, e.g. "ml-IN"
    language_probability: float | None = None  # 0–1, how sure the detector is of the language


class STTProvider(Protocol):
    def transcribe(self, audio: bytes, mime_type: str) -> Transcript:
        """Raises STTError on failure."""
        ...
