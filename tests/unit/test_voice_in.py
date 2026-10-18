from __future__ import annotations

import json
import struct
import subprocess
import wave
from io import BytesIO

import httpx
import pytest
import respx

from app.conversation.voice_in import VoiceIn, is_unclear
from app.providers.audio import find_ffmpeg, ogg_duration_seconds, split_to_wav_chunks
from app.providers.stt import MockSTT, SarvamSTT, STTError, Transcript
from app.whatsapp.client import WhatsAppClient, WhatsAppError

SARVAM = "https://api.sarvam.ai/speech-to-text"


def ogg_page(granule: int, payload: bytes = b"") -> bytes:
    """Minimal OGG page header: capture pattern, version 0, flags, granule, serial, seq, crc, 0 segments."""
    return b"OggS" + bytes([0, 0]) + struct.pack("<q", granule) + b"\0" * 12 + b"\0" + payload


def fake_voice_note(seconds: float, pre_skip: int = 312) -> bytes:
    head = b"OpusHead" + bytes([1, 1]) + struct.pack("<H", pre_skip) + b"\0" * 7
    return ogg_page(0, head) + ogg_page(int(seconds * 48_000) + pre_skip)


# --------------------------------------------------------------------------- audio helpers


def test_ogg_duration_from_page_headers() -> None:
    assert ogg_duration_seconds(fake_voice_note(12.5)) == pytest.approx(12.5)
    assert ogg_duration_seconds(b"RIFF....WAVE") is None
    assert ogg_duration_seconds(b"OggS-garbage-that-is-not-a-page") is None


@pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg not installed")
def test_real_voice_note_duration_and_split(tmp_path) -> None:
    ogg = tmp_path / "note.ogg"
    subprocess.run([find_ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=65",
                    "-ac", "1", "-c:a", "libopus", str(ogg)], check=True)
    data = ogg.read_bytes()
    assert ogg_duration_seconds(data) == pytest.approx(65, abs=0.1)
    chunks = split_to_wav_chunks(data, 28, find_ffmpeg())
    durations = []
    for chunk in chunks:
        with wave.open(BytesIO(chunk)) as w:
            assert (w.getnchannels(), w.getframerate()) == (1, 16000)
            durations.append(w.getnframes() / w.getframerate())
    assert len(chunks) == 3 and max(durations) <= 28.5 and sum(durations) == pytest.approx(65, abs=0.5)


# --------------------------------------------------------------------------- Sarvam


def ok(text: str, lang: str = "ml-IN", p: float = 1.0) -> httpx.Response:
    return httpx.Response(200, json={"request_id": "r", "transcript": text, "language_code": lang,
                                     "language_probability": p})


@respx.mock
def test_sarvam_request_shape() -> None:
    route = respx.post(SARVAM).mock(return_value=ok("സ്വിഫ്റ്റ് ഉണ്ടോ"))
    stt = SarvamSTT("key", model="saaras:v4", keyterms=["Swift", "Creta"])
    result = stt.transcribe(fake_voice_note(5), "audio/ogg; codecs=opus")
    assert result == Transcript("സ്വിഫ്റ്റ് ഉണ്ടോ", "ml-IN", 1.0)
    request = route.calls[0].request
    body = request.content.decode("latin-1")
    assert request.headers["api-subscription-key"] == "key"
    assert 'filename="voice.ogg"' in body and "Content-Type: audio/ogg\r\n" in body  # codecs param dropped
    assert "saaras:v4" in body and "unknown" in body and json.dumps(["Swift", "Creta"]) in body


@respx.mock
def test_long_note_is_split_and_joined() -> None:
    route = respx.post(SARVAM).mock(side_effect=[ok("first part", "en-IN", 0.7), ok("second part", "ml-IN", 0.95)])
    pieces: list[float] = []

    def splitter(audio: bytes, seconds: float) -> list[bytes]:
        pieces.append(seconds)
        return [b"wav1", b"wav2"]

    result = SarvamSTT("k", splitter=splitter).transcribe(fake_voice_note(45), "audio/ogg")
    assert result == Transcript("first part second part", "ml-IN", 0.95)
    assert pieces == [28] and route.call_count == 2


@respx.mock
def test_too_long_without_ffmpeg_or_unknown_length() -> None:
    with pytest.raises(STTError) as info:
        SarvamSTT("k").transcribe(fake_voice_note(45), "audio/ogg")
    assert info.value.too_long
    respx.post(SARVAM).mock(return_value=httpx.Response(400, json={"error": {
        "message": "Audio duration exceeds the maximum limit of 30 seconds."}}))
    with pytest.raises(STTError) as info:
        SarvamSTT("k").transcribe(b"ID3 mp3 of unknown length", "audio/mpeg")
    assert info.value.too_long


@respx.mock
def test_sarvam_errors() -> None:
    respx.post(SARVAM).mock(return_value=httpx.Response(403, json={"error": {"message": "Invalid API key"}}))
    with pytest.raises(STTError, match="403: Invalid API key") as info:
        SarvamSTT("bad").transcribe(fake_voice_note(3), "audio/ogg")
    assert not info.value.too_long
    respx.post(SARVAM).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(STTError, match="request failed"):
        SarvamSTT("k").transcribe(fake_voice_note(3), "audio/ogg")


# --------------------------------------------------------------------------- unclear check


@pytest.mark.parametrize(("transcript", "unclear"), [
    (Transcript(""), True),
    (Transcript("   "), True),
    (Transcript("എനിക്ക് ഒരു കാർ വേണം", "ml-IN", 1.0), False),
    (Transcript("ശരി", "ml-IN", 0.6), False),          # a yes to a read-back
    (Transcript("sheri", "en-IN", 0.5), False),
    (Transcript("എട്ട് ലക്ഷം", "ml-IN", 0.9), False),  # a number
    (Transcript("Swift", "en-IN", 0.95), False),
    (Transcript("hmm", "en-IN", 0.5), True),           # one doubtful word
    (Transcript("some words here", None, 0.2), True),  # language detector unsure
])
def test_is_unclear(transcript: Transcript, unclear: bool) -> None:
    assert is_unclear(transcript) is unclear


# --------------------------------------------------------------------------- VoiceIn + download

GRAPH = "https://graph.facebook.com/v25.0"
MEDIA_URL = "https://lookaside.fbsbx.com/m1"


@respx.mock
def test_download_media_two_step() -> None:
    respx.get(f"{GRAPH}/m1").mock(return_value=httpx.Response(200, json={
        "url": MEDIA_URL, "mime_type": "audio/ogg; codecs=opus", "file_size": 5}))
    fetch = respx.get(MEDIA_URL).mock(return_value=httpx.Response(200, content=b"audio"))
    assert WhatsAppClient("1", "tok").download_media("m1") == (b"audio", "audio/ogg; codecs=opus")
    assert fetch.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_download_media_rejects_huge_or_missing() -> None:
    respx.get(f"{GRAPH}/big").mock(return_value=httpx.Response(200, json={"url": MEDIA_URL, "file_size": 99_000_000}))
    with pytest.raises(WhatsAppError, match="too large"):
        WhatsAppClient("1", "t").download_media("big")
    respx.get(f"{GRAPH}/gone").mock(return_value=httpx.Response(404, text="not found"))
    with pytest.raises(WhatsAppError, match="HTTP 404"):
        WhatsAppClient("1", "t").download_media("gone")


class FakeWhatsApp:
    def __init__(self, audio: bytes | Exception) -> None:
        self.audio = audio

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        if isinstance(self.audio, Exception):
            raise self.audio
        return self.audio, "audio/ogg; codecs=opus"


def test_voice_in_outcomes() -> None:
    stt = MockSTT([Transcript("Creta undo?", "en-IN", 0.9)])
    assert VoiceIn(FakeWhatsApp(fake_voice_note(4)), stt).process("m").text == "Creta undo?"
    long_note = VoiceIn(FakeWhatsApp(fake_voice_note(75)), MockSTT(), max_seconds=60).process("m")
    assert long_note.problem == "too_long" and long_note.duration_s == pytest.approx(75)
    assert VoiceIn(FakeWhatsApp(WhatsAppError("down")), MockSTT()).process("m").problem == "failed"
    assert VoiceIn(FakeWhatsApp(b"x"), MockSTT()).process(None).problem == "failed"
    unclear = VoiceIn(FakeWhatsApp(fake_voice_note(2)), MockSTT([Transcript("", "ml-IN", 1.0)])).process("m")
    assert unclear.problem == "unclear"
