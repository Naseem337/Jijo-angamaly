from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

import app.conversation.voice_out as voice_out_module
from app.agent import Agent
from app.agent.spoken import to_speech
from app.conversation.voice_out import VoiceOut
from app.core.client_config import load_client_config
from app.core.clock import FixedClock
from app.inventory import build_inventory_adapter
from app.providers.audio import find_ffmpeg, ogg_duration_seconds, wav_to_ogg_opus
from app.providers.llm import MockLLM
from app.providers.tts import MockTTS, TTSError
from app.storage.models import Message
from app.storage.sqlite_repo import SQLiteRepository
from app.whatsapp.client import WhatsAppClient, WhatsAppError

PHONE = "+919847012345"


# --------------------------------------------------------------------------- text → speech text


@pytest.mark.parametrize(("text", "language", "expected"), [
    ("*2021 Hyundai Creta* (CAR242) – ₹9,80,000, 60,400 km. 😊",
     "manglish", "ട്വന്റി ട്വന്റി വൺ Hyundai Creta – ഒമ്പത് ലക്ഷത്തി എണ്പതിനായിരം, അറുപതിനായിരം കിലോമീറ്റർ."),
    ("ബജറ്റ് 10 lakh രൂപ, Rs 6,95,000 വില", "ml", "ബജറ്റ് പത്ത് ലക്ഷം, ആറ് ലക്ഷത്തി തൊണ്ണൂറ്റിയഞ്ചായിരം വില"),
    ("2007 മോഡൽ, 8,500 km, ₹85,000", "ml", "ടൂ തൗസൻഡ് സെവൻ മോഡൽ, എണ്ണായിരത്തിയഞ്ഞൂറ് കിലോമീറ്റർ, എണ്പത്തിയഞ്ചായിരം രൂപ"),
    ("2 ഉടമകൾ, 1.5 ലിറ്റർ", "ml", "രണ്ട് ഉടമകൾ, ഒന്ന് പോയിന്റ് അഞ്ച് ലിറ്റർ"),
    ("*2021 Creta* (TC-1002) – ₹9,80,000 😊", "en", "2021 Creta – Rupees 9,80,000"),
])
def test_to_speech(text: str, language: str, expected: str) -> None:
    assert to_speech(text, language) == expected


# --------------------------------------------------------------------------- agent spoken_text


@pytest.fixture
def repo(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "voice.db")
    yield repository
    repository.close()


@pytest.fixture
def agent_with(clients_dir: Path, repo: SQLiteRepository, clock: FixedClock):
    config = load_client_config("truechoice", clients_dir)

    def make(*script) -> tuple[Agent, MockLLM]:
        llm = MockLLM(list(script))
        return Agent(config, llm, repo, build_inventory_adapter(config), clock), llm
    return make


def test_voice_reply_gets_checked_spoken_version(agent_with) -> None:
    agent, llm = agent_with(
        MockLLM.tool("search_cars", model="swift", budget_max=700000),
        MockLLM.text("Swift und: *2021 Maruti Suzuki Swift ZXi AMT* – ₹6,95,000, 28,500 km (TC-1002)"),
        MockLLM.text("2021 Swift ₹6,95,000-ന് ഉണ്ട്, 28,500 km ഓടിയത്. വിവരങ്ങൾ മെസ്സേജിൽ അയച്ചിട്ടുണ്ട്."),
    )
    reply = agent.handle(PHONE, "Swift undo 7 lakhinu thazhe?", source="voice", want_voice=True)
    assert reply.spoken_text == ("ട്വന്റി ട്വന്റി വൺ Swift ആറ് ലക്ഷത്തി തൊണ്ണൂറ്റിയഞ്ചായിരം-ന് ഉണ്ട്, "
                                 "ഇരുപത്തിയെട്ടായിരം കിലോമീറ്റർ ഓടിയത്. വിവരങ്ങൾ മെസ്സേജിൽ അയച്ചിട്ടുണ്ട്.")
    spoken_call = llm.calls[2]
    assert "Malayalam script" in spoken_call.system and spoken_call.tools == []
    assert spoken_call.messages[0].content == reply.text


def test_spoken_version_with_invented_number_is_dropped(agent_with) -> None:
    agent, _ = agent_with(MockLLM.text("Namaskaram! Ethu car aanu nokkunnath?"),
                          MockLLM.text("നമസ്കാരം! ₹4 ലക്ഷത്തിന് ഒരു കാർ ഉണ്ട്."))
    reply = agent.handle(PHONE, "hi", source="voice", want_voice=True)
    assert reply.guardrail == "ok" and reply.spoken_text is None


def test_apologies_are_never_spoken(agent_with) -> None:
    agent, llm = agent_with(MockLLM.text("Loan guaranteed!"), MockLLM.text("Still guaranteed!"))
    reply = agent.handle(PHONE, "loan kittumo?", source="voice", want_voice=True)
    assert reply.guardrail == "fallback" and reply.spoken_text is None
    assert len(llm.calls) == 2  # no spoken-version call either


def test_no_spoken_text_unless_asked_and_voice_copies_skip_history(agent_with, repo, clock) -> None:
    repo.add_message(Message("truechoice", PHONE, "in", "hi", clock.now()))
    repo.add_message(Message("truechoice", PHONE, "out", "Hello!", clock.now()))
    repo.add_message(Message("truechoice", PHONE, "out", "ഹലോ!", clock.now(), source="voice", meta={"voice_note": True}))
    agent, llm = agent_with(MockLLM.text("Sure."))
    assert agent.handle(PHONE, "SUV venam").spoken_text is None
    assert [m.content for m in llm.calls[0].messages[:2]] == ["hi", "Hello!"]


# --------------------------------------------------------------------------- VoiceOut


class FakeWhatsApp:
    def __init__(self, fail: Exception | None = None) -> None:
        self.uploads: list[bytes] = []
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def upload_media(self, data: bytes, mime_type: str = "audio/ogg", filename: str = "voice.ogg") -> str:
        if self.fail:
            raise self.fail
        self.uploads.append(data)
        return f"media-{len(self.uploads)}"

    def send_audio(self, to: str, media_id: str, *, voice: bool = True) -> str:
        self.sent.append((to, media_id))
        return "wamid.audio"


@pytest.fixture
def voice_out(monkeypatch: pytest.MonkeyPatch, clients_dir: Path, repo, clock):
    monkeypatch.setattr(voice_out_module, "wav_to_ogg_opus", lambda wav, ffmpeg: b"OggS" + wav)
    config = load_client_config("truechoice", clients_dir)

    def make(tts=None, whatsapp=None, cap=None, policy=None) -> VoiceOut:
        cfg = config
        if cap is not None or policy is not None:
            voice = config.voice.model_copy(update={k: v for k, v in
                                                    {"daily_char_cap": cap, "policy": policy}.items() if v is not None})
            cfg = config.model_copy(update={"voice": voice})
        return VoiceOut(cfg, tts or MockTTS(), whatsapp or FakeWhatsApp(), repo, clock, "ffmpeg")
    return make


def test_voice_note_is_made_uploaded_sent_and_cached(voice_out, repo) -> None:
    tts, wa = MockTTS(), FakeWhatsApp()
    vo = voice_out(tts, wa)
    assert vo.send(PHONE, "നമസ്കാരം", "manglish")
    assert vo.send(PHONE, "നമസ്കാരം", "ml")  # same phrase: cached media id, no new TTS/upload
    assert tts.calls == [("നമസ്കാരം", "ml-IN")]
    assert wa.uploads == [b"OggSRIFF-fake-wav:" + "നമസ്കാരം".encode()]
    assert wa.sent == [(PHONE, "media-1"), (PHONE, "media-1")]
    assert repo.get_counter("truechoice", "tts_chars", "2026-10-16") == len("നമസ്കാരം")
    stored = repo.list_messages("truechoice", PHONE)
    assert stored[-1].source == "voice" and stored[-1].meta == {"voice_note": True}


def test_english_voice_and_daily_cap(voice_out) -> None:
    tts = MockTTS()
    vo = voice_out(tts, cap=20)
    assert vo.send(PHONE, "Hello there", "en") and tts.calls[0][1] == "en-IN"
    assert not vo.send(PHONE, "This one is over the cap", "en")
    assert len(tts.calls) == 1


@pytest.mark.parametrize("failure", ["tts", "upload"])
def test_failures_cost_only_the_voice_note(voice_out, failure) -> None:
    wa = FakeWhatsApp(WhatsAppError("upload failed") if failure == "upload" else None)
    tts = MockTTS(TTSError("Sarvam down") if failure == "tts" else None)
    assert voice_out(tts, wa).send(PHONE, "നമസ്കാരം", "ml") is False
    assert wa.sent == []


def test_voice_policy(voice_out) -> None:
    assert voice_out().wants_voice("voice") and not voice_out().wants_voice("text")
    assert voice_out(policy="always").wants_voice("text")
    assert not voice_out(policy="never").wants_voice("voice")


# --------------------------------------------------------------------------- real ffmpeg + WhatsApp API


@pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg not installed")
def test_wav_to_ogg_opus_with_real_ffmpeg(tmp_path) -> None:
    wav = tmp_path / "tone.wav"
    subprocess.run([find_ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-ar", "24000", str(wav)], check=True)
    ogg = wav_to_ogg_opus(wav.read_bytes(), find_ffmpeg())
    assert ogg.startswith(b"OggS") and b"OpusHead" in ogg[:200]
    assert ogg_duration_seconds(ogg) == pytest.approx(3, abs=0.1)
    assert len(ogg) < 30_000  # 32 kbit/s speech: tiny, well under WhatsApp's 512 KB play-button limit


@respx.mock
def test_upload_and_send_voice_note() -> None:
    upload = respx.post("https://graph.facebook.com/v25.0/123/media").mock(
        return_value=httpx.Response(200, json={"id": "media-9"}))
    send = respx.post("https://graph.facebook.com/v25.0/123/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.a"}]}))
    wa = WhatsAppClient("123", "tok")
    assert wa.upload_media(b"OggS...", "audio/ogg") == "media-9"
    body = upload.calls[0].request.content
    assert b'name="messaging_product"' in body and b"audio/ogg" in body and b"OggS..." in body
    wa.send_audio(PHONE, "media-9")
    assert json.loads(send.calls[0].request.content)["audio"] == {"id": "media-9", "voice": True}
    upload.mock(return_value=httpx.Response(400, json={"error": {"message": "bad file"}}))
    with pytest.raises(WhatsAppError, match="upload HTTP 400"):
        wa.upload_media(b"x")


def test_speaking_style_reaches_text_and_voice_prompts(agent_with) -> None:
    agent, llm = agent_with(MockLLM.text("Namaskaram! Ethu car aanu nokkunnath?"),
                            MockLLM.text("നമസ്കാരം! ഏത് കാർ ആണ് നോക്കുന്നത്?"))
    agent.handle(PHONE, "hi", source="voice", want_voice=True)
    style = agent.config.business_rules.speaking_style.strip()
    assert f"Tone and dialect: {style}" in llm.calls[0].system
    assert llm.calls[1].system.endswith(f"Tone and dialect: {style}")


# --------------------------------------------------------------------------- Gemini voice + backup


@respx.mock
def test_gemini_tts_request_and_audio_formats() -> None:
    import base64, io, wave
    from app.providers.tts import GeminiTTS
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-tts:generateContent"
    pcm = b"\x00\x01" * 2400
    route = respx.post(url).mock(return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]}))
    tts = GeminiTTS("key", model="gemini-tts", speaker="Algenib", style="Speak like a calm 45-year-old.")
    wav = tts.synthesize("നമസ്കാരം", "ml-IN")
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())) == (24000, 1, pcm)
    request = route.calls[0].request
    body = json.loads(request.content)
    assert request.headers["x-goog-api-key"] == "key" and "key=" not in str(request.url)
    # the style must never be in the spoken text (it was read aloud when it was)
    assert body["contents"][0]["parts"][0]["text"] == "നമസ്കാരം"
    assert body["systemInstruction"]["parts"][0]["text"] == "Speak like a calm 45-year-old."
    assert body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Algenib"
    route.mock(return_value=httpx.Response(429, json={"error": {"message": "quota exceeded"}}))
    with pytest.raises(TTSError, match="429: quota exceeded"):
        tts.synthesize("hi", "en-IN")


def test_backup_voice_speaks_when_main_voice_fails(voice_out) -> None:
    main, backup, wa = MockTTS(TTSError("Gemini 429")), MockTTS(), FakeWhatsApp()
    vo = voice_out(None, wa)
    vo.voices = [main, backup]
    assert vo.send(PHONE, "നമസ്കാരം", "ml")
    assert main.calls and backup.calls and wa.sent == [(PHONE, "media-1")]
    both_fail = voice_out(None, FakeWhatsApp())
    both_fail.voices = [MockTTS(TTSError("down")), MockTTS(TTSError("down"))]
    assert both_fail.send(PHONE, "ഹലോ", "ml") is False


def test_truechoice_voice_order(clients_dir) -> None:
    from app.providers.tts import GeminiTTS, SarvamTTS, build_tts

    class Keys:
        def get(self, name: str) -> str:
            return "k"
    voices = build_tts(load_client_config("truechoice", clients_dir), Keys())
    assert [(type(v), v.speaker) for v in voices] == [(SarvamTTS, "anand")]  # Gemini off until verified
