"""Scripted speech-to-text for tests."""

from __future__ import annotations

from app.providers.stt.base import STTError, Transcript


class MockSTT:
    def __init__(self, script: list[Transcript | STTError] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[tuple[bytes, str]] = []

    def transcribe(self, audio: bytes, mime_type: str) -> Transcript:
        self.calls.append((audio, mime_type))
        if not self.script:
            raise AssertionError("MockSTT has no scripted transcript left")
        item = self.script.pop(0)
        if isinstance(item, STTError):
            raise item
        return item
