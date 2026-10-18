"""Text-to-speech stand-in for tests: returns fake WAV bytes and records what it was asked."""

from __future__ import annotations

from app.providers.tts.base import TTSError


class MockTTS:
    model = "mock-tts"
    speaker = "mock"

    def __init__(self, fail: TTSError | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def synthesize(self, text: str, language_code: str) -> bytes:
        self.calls.append((text, language_code))
        if self.fail:
            raise self.fail
        return b"RIFF-fake-wav:" + text.encode()
