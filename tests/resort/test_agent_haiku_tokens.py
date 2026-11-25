"""Claude Haiku provider, prompt caching and lean requests, with FAKE clients only (no real API call anywhere)."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from types import SimpleNamespace

import anthropic
import httpx
from test_agent_turn import ASK, STAY, go
from test_bot import GUEST, reply, tool, world  # noqa: F401  (world is a fixture)

from app.providers.llm import AnthropicLLM, FallbackLLM, MockLLM, build_llm
from app.providers.llm.anthropic import request
from app.providers.llm.base import CACHE_SPLIT, ChatMessage, LLMResponse, Usage, plain_system
from app.providers.llm.openai_compatible import _to_wire as openai_wire
from app.resort.agent import usage
from app.resort.agent.prompt import build_system_prompt, static_prompt
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import TOOL_SCHEMAS
from app.resort.agent.turn import HISTORY, compact_result, summary_line
from app.resort.config import load_resort_config
from app.settings import EnvSecretResolver


def system_of(w, phone=GUEST):
    return build_system_prompt(w.bot, phone, Enquiry.load(w.repo, phone))


# --------------------------------------------------------------------------- 2. prompt caching


def test_the_cache_breakpoints_are_after_the_tools_and_after_the_static_system(world):
    kw = request("model-from-env", system_of(world), [ChatMessage("user", "hi")], TOOL_SCHEMAS, 300, 0.2)
    static, dynamic = kw["system"]
    assert static["cache_control"] == {"type": "ephemeral"} and "cache_control" not in dynamic
    assert "OWNER RULES" in static["text"] and "FACTS" in static["text"]  # FACTS stays in the cached part
    today = world.bot._today().isoformat()
    assert "\nTODAY\n" not in static["text"] and "\nCURRENT ENQUIRY\n" not in static["text"] and today not in static["text"]
    assert today in dynamic["text"]
    assert dynamic["text"].startswith("TODAY") and "CURRENT ENQUIRY" in dynamic["text"]
    assert [t.get("cache_control") for t in kw["tools"]] == [None] * (len(TOOL_SCHEMAS) - 1) + [{"type": "ephemeral"}]
    assert "temperature" not in kw and kw["max_tokens"] == 300  # Claude's API (2026-10-21) rejects sampling params
    assert CACHE_SPLIT not in static["text"] + dynamic["text"]


def test_nothing_per_guest_or_per_turn_is_in_the_cached_part(world):
    w = world
    first = static_prompt(w.bot)
    go(w, ASK, tool("update_enquiry", **STAY), reply("Done."))  # this guest now has an enquiry
    w.clock.advance(hours=30)  # another day
    assert static_prompt(w.bot) == first
    assert system_of(w).split(CACHE_SPLIT)[0] == system_of(w, "+919800000123").split(CACHE_SPLIT)[0] == first
    assert system_of(w).split(CACHE_SPLIT)[1] != system_of(w, "+919800000123").split(CACHE_SPLIT)[1]


def test_providers_without_caching_get_one_plain_system_text(world):
    system = system_of(world)
    wire = openai_wire(system, [ChatMessage("user", "hi")])
    assert wire[0]["content"] == plain_system(system) and "CACHE_SPLIT" not in wire[0]["content"]


# --------------------------------------------------------------------------- 3. smaller requests


def test_the_history_is_the_last_12_real_messages(world):
    w = world
    for i in range(30):
        w.repo.add_chat(__import__("app.resort.models", fromlist=["ChatMessage"]).ChatMessage(
            GUEST, "in" if i % 2 == 0 else "out", f"message {i}", w.clock.now() - timedelta(minutes=100 - i), "text"))
    go(w, "the new one", reply("ok"))
    lines = [ln for m in w.llm.calls[0].messages for ln in m.content.splitlines()]
    assert HISTORY == 12 and len(lines) <= 13 and "message 17" not in lines and "message 29" in lines


def test_an_earlier_rounds_tool_results_shrink_to_one_line(world):
    w = world
    go(w, ASK + " price?", tool("update_enquiry", call_id="a", **STAY), tool("get_price", call_id="b"), reply("ok"))
    third = [m for m in w.llm.calls[2].messages if m.role == "tool"]
    assert third[0].tool_call_id == "a" and third[0].content.startswith("[earlier result] update_enquiry: ok")
    assert "\n" not in third[0].content and "next ask_contact" in third[0].content
    assert json.loads(third[1].content)["ok"] is not None  # the newest round stays in full


def test_tool_results_are_compact_json_without_empty_fields():
    out = {"ok": False, "facts": {"room": "Deluxe", "empty": "", "none": None}, "sent": [], "next_step": None,
           "note": "Ask again."}
    text = compact_result(out)
    assert json.loads(text) == {"ok": False, "facts": {"room": "Deluxe"}, "note": "Ask again."}
    assert ", " not in text and ": " not in text
    line = summary_line("update_enquiry", {"ok": True, "facts": {"status": "rooms_ok", "rooms": 2},
                                           "sent": ["rooms_ok", "ask_contact", "staff:x"], "next_step": "ask_contact"})
    assert line == ("[earlier result] update_enquiry: ok, status=rooms_ok, rooms=2, sent rooms_ok, ask_contact, "
                    "next ask_contact")


# --------------------------------------------------------------------------- 1. provider + fallback


class FakeMessages:
    def __init__(self, error=None, response=None):
        self.error, self.response, self.kwargs = error, response, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.response


def claude(error=None, response=None):
    fake = FakeMessages(error, response)
    return AnthropicLLM("anthropic", model="model-from-env", api_key="unused", client=SimpleNamespace(messages=fake)), fake


def test_an_anthropic_error_falls_back_to_sarvam_then_groq():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    for error in (anthropic.APIConnectionError(request=req),
                  anthropic.InternalServerError("overloaded", response=httpx.Response(529, request=req), body=None),
                  anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)):
        first, _ = claude(error)
        sarvam = MockLLM([MockLLM.text("Hi.")], name="sarvam_chat")
        chain = FallbackLLM([first, sarvam, MockLLM([], name="groq")])
        out = chain.complete("static" + CACHE_SPLIT + "dynamic", [ChatMessage("user", "hi")], TOOL_SCHEMAS, 300,
                             temperature=0.2)
        assert out.provider == "sarvam_chat" and out.text == "Hi."
        assert sarvam.calls[0].temperature == 0.2


class Broken:
    """A provider with a bug: it raises a plain TypeError, not an LLMError."""
    name = "anthropic"

    def complete(self, *a, **k):
        raise TypeError("Messages.create() got an unexpected keyword argument 'temperature'")


def test_a_provider_that_raises_typeerror_hands_the_turn_to_the_next_provider(world, caplog):
    w = world
    w.llm.script.extend([tool("update_enquiry", **STAY), reply("Sure.")])
    chain = FallbackLLM([Broken(), w.llm])
    from app.resort.agent.outbox import bot_outbox
    from app.resort.agent.turn import run_turn
    from app.resort.models import ChatMessage as Row
    w.repo.add_chat(Row(GUEST, "in", ASK, w.clock.now(), "text"))
    with caplog.at_level(logging.WARNING):
        res = run_turn(w.bot, chain, GUEST, ASK, bot_outbox(w.bot, GUEST, dry_run=True))
    assert res.outcome != "ai_down" and [c["name"] for c in res.calls] == ["update_enquiry"]
    crashed = [r.getMessage() for r in caplog.records if "crashed, trying next" in r.getMessage()]
    assert crashed and all("\n" not in m for m in crashed) and "TypeError" in crashed[0]


def test_any_sdk_exception_becomes_an_llm_error_for_the_chain():
    from app.providers.llm import LLMError
    llm, _ = claude(ValueError("odd SDK change"))
    try:
        llm.complete("s", [ChatMessage("user", "hi")], None, 300)
    except LLMError as exc:
        assert "ValueError" in str(exc) and not exc.retryable
    else:
        raise AssertionError("expected LLMError")


def test_the_resort_config_turns_extended_thinking_off_for_claude(monkeypatch):
    monkeypatch.setenv("LLM_CHAIN", "anthropic")
    assert load_resort_config().providers.llm["anthropic"].extra == {"thinking": {"type": "disabled"}}


def test_anthropic_reports_cache_reads_and_writes():
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="Hi.")], stop_reason="end_turn",
                               model="model-from-env", usage=SimpleNamespace(input_tokens=50, output_tokens=7,
                                                                             cache_read_input_tokens=4000,
                                                                             cache_creation_input_tokens=300))
    llm, fake = claude(response=response)
    out = llm.complete("s" + CACHE_SPLIT + "d", [ChatMessage("user", "hi")], TOOL_SCHEMAS, 300, temperature=0.2)
    assert out.usage == Usage(4350, 7, 4000, 300) and out.text == "Hi."
    assert fake.kwargs["model"] == "model-from-env" and fake.kwargs["system"][0]["text"] == "s"
    assert "temperature" not in fake.kwargs  # asked for 0.2, never sent to Claude


def test_a_missing_key_skips_anthropic_with_a_clear_log_line(monkeypatch, caplog):
    monkeypatch.setenv("LLM_CHAIN", "anthropic,sarvam_chat")
    monkeypatch.setenv("AGENT_MODEL", "model-from-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("SARVAM_API_KEY", "fake-sarvam-key")
    with caplog.at_level(logging.WARNING):
        chain = build_llm(load_resort_config(), EnvSecretResolver(read_dotenv_file=False))
    assert [p.name for p in chain.providers] == ["sarvam_chat"]
    assert any("LLM provider skipped: anthropic" in r.getMessage() and "ANTHROPIC_API_KEY" in r.getMessage()
               for r in caplog.records)


def test_the_model_comes_from_agent_model(monkeypatch):
    monkeypatch.setenv("LLM_CHAIN", "anthropic")
    monkeypatch.setenv("AGENT_MODEL", "whatever-the-env-says")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
    chain = build_llm(load_resort_config(), EnvSecretResolver(read_dotenv_file=False))
    assert chain.providers[0].name == "anthropic" and chain.providers[0].model == "whatever-the-env-says"


# --------------------------------------------------------------------------- 4. measure


def test_a_turn_adds_up_its_tokens_and_logs_no_guest_text(world, tmp_path, monkeypatch, caplog):
    w = world
    log_file = tmp_path / "tokens.jsonl"
    monkeypatch.setenv("AGENT_TOKEN_LOG", str(log_file))
    first = MockLLM.tool("update_enquiry", call_id="a", **STAY)
    first.usage, first.model = Usage(4200, 30, 4000, 0), "model-x"
    last = LLMResponse("Done.", usage=Usage(4500, 5, 4000, 200), model="model-x")
    with caplog.at_level(logging.INFO):
        res, _ = go(w, ASK + " secret words", first, last, live=True)
    assert (res.llm_calls, res.input_tokens, res.cache_read_tokens, res.cache_write_tokens, res.output_tokens) == \
        (2, 8700, 8000, 200, 35)
    row = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert row["model"] == "model-x" and row["rounds"] == 1 and row["tools"] == ["update_enquiry"]
    assert row["chat"] == usage.chat_id(GUEST) and GUEST not in json.dumps(row) and "secret" not in json.dumps(row)
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Agent tokens:"))
    assert "cache_read=8000" in line and "secret" not in line and GUEST[-4:] not in line


def test_the_token_report_adds_up_turns_and_booking_chats(tmp_path):
    from scripts.token_report import load, report
    rows = [{"at": "2026-11-25T10:00:00+05:30", "chat": "a", "model": "m", "llm_calls": 2, "input": 1000,
             "cache_read": 800, "cache_write": 0, "output": 20, "rounds": 1, "booking": False},
            {"at": "2026-11-25T10:01:00+05:30", "chat": "a", "model": "m", "llm_calls": 2, "input": 1000,
             "cache_read": 800, "cache_write": 0, "output": 20, "rounds": 1, "booking": True},
            {"at": "2026-11-25T10:02:00+05:30", "chat": "b", "model": "m", "llm_calls": 1, "input": 500,
             "cache_read": 0, "cache_write": 400, "output": 10, "rounds": 0, "booking": False}]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    text = "\n".join(report(load(path)))
    assert "Turns: 3" in text and "booking chats: 1" in text
    assert "Average per turn: 850 tokens" in text and "Average per booking chat: 2,040 tokens" in text
    assert "hit rate 64%" in text
