from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
import respx

from app.core.client_config import load_client_config
from app.providers.llm import (
    AnthropicLLM,
    ChatMessage,
    FallbackLLM,
    LLMError,
    LLMNotConfigured,
    LLMResponse,
    MockLLM,
    OpenAICompatibleLLM,
    ToolCall,
    ToolSpec,
    build_llm,
)
from app.settings import SecretNotFound

URL = "https://api.example.com/chat/completions"
SEARCH = ToolSpec("search_cars", "Search stock", {"type": "object", "properties": {"model": {"type": "string"}}})


def openai_llm() -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM("deepseek", base_url="https://api.example.com/", model="deepseek-chat",
                               api_key="key", extra={"temperature": 0.3})


def conversation() -> list[ChatMessage]:
    """user -> assistant calls two tools -> two tool results."""
    return [
        ChatMessage.user("Swift under 6 lakh?"),
        ChatMessage("assistant", "", [ToolCall("c1", "search_cars", {"model": "swift"}),
                                      ToolCall("c2", "search_cars", {"model": "baleno"})]),
        ChatMessage.tool_result("c1", '[{"id": "TC-1001"}]'),
        ChatMessage.tool_result("c2", "no cars", is_error=True),
    ]


# --------------------------------------------------------------------------- OpenAI-compatible


@respx.mock
def test_openai_compatible_request_shape_and_text_reply() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={
        "model": "deepseek-chat",
        "choices": [{"message": {"content": "Namaskaram!"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5, "prompt_cache_hit_tokens": 40},
    }))
    reply = openai_llm().complete("SYSTEM", conversation(), [SEARCH], max_tokens=300)

    body = json.loads(route.calls[0].request.content)
    assert route.calls[0].request.headers["Authorization"] == "Bearer key"
    assert body["model"] == "deepseek-chat" and body["max_tokens"] == 300 and body["temperature"] == 0.3
    assert body["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert body["messages"][2]["tool_calls"][1]["function"] == {"name": "search_cars", "arguments": '{"model": "baleno"}'}
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": '[{"id": "TC-1001"}]'}
    assert body["tools"][0]["function"]["name"] == "search_cars"
    assert (reply.text, reply.stop_reason, reply.provider) == ("Namaskaram!", "end", "deepseek")
    assert (reply.usage.input_tokens, reply.usage.output_tokens, reply.usage.cached_input_tokens) == (50, 5, 40)


@respx.mock
def test_openai_compatible_tool_calls_including_bad_arguments() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={"choices": [{
        "message": {"content": None, "tool_calls": [
            {"id": "a", "function": {"name": "search_cars", "arguments": '{"model": "city"}'}},
            {"id": "b", "function": {"name": "search_cars", "arguments": "{oops"}},
        ]},
        "finish_reason": "tool_calls",
    }]}))
    reply = openai_llm().complete("S", [ChatMessage.user("hi")], [SEARCH])
    assert reply.stop_reason == "tool_calls" and reply.text == ""
    assert reply.tool_calls[0] == ToolCall("a", "search_cars", {"model": "city"})
    assert reply.tool_calls[1].arguments == {} and "not valid JSON" in reply.tool_calls[1].arguments_error


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (401, False), (400, False)])
def test_openai_compatible_http_errors(status: int, retryable: bool) -> None:
    respx.post(URL).mock(return_value=httpx.Response(status, text="nope"))
    with pytest.raises(LLMError, match=f"HTTP {status}") as info:
        openai_llm().complete("S", [ChatMessage.user("hi")])
    assert info.value.retryable is retryable and info.value.provider == "deepseek"


@respx.mock
def test_openai_compatible_network_error_and_garbage() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(LLMError, match="request failed"):
        openai_llm().complete("S", [ChatMessage.user("hi")])
    respx.post(URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(LLMError, match="unexpected response"):
        openai_llm().complete("S", [ChatMessage.user("hi")])


# --------------------------------------------------------------------------- Anthropic


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def claude_response(*blocks: Any, stop: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks), stop_reason=stop, model="claude-test",
        usage=SimpleNamespace(input_tokens=10, output_tokens=4,
                              cache_creation_input_tokens=0, cache_read_input_tokens=90),
    )


def claude(result: Any) -> tuple[AnthropicLLM, FakeMessages]:
    messages = FakeMessages(result)
    llm = AnthropicLLM("anthropic", model="claude-test", api_key="k",
                       client=SimpleNamespace(messages=messages))
    return llm, messages


def test_anthropic_request_shape_caching_and_merged_tool_results() -> None:
    llm, fake = claude(claude_response(SimpleNamespace(type="text", text="Here you go")))
    reply = llm.complete("SYSTEM", conversation(), [SEARCH], max_tokens=500)

    kw = fake.kwargs
    assert kw["model"] == "claude-test" and kw["max_tokens"] == 500
    assert kw["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert kw["tools"] == [{"name": "search_cars", "description": "Search stock", "input_schema": SEARCH.parameters}]
    assistant, results = kw["messages"][1], kw["messages"][2]
    assert assistant["content"][0] == {"type": "tool_use", "id": "c1", "name": "search_cars", "input": {"model": "swift"}}
    assert len(kw["messages"]) == 3  # both tool results share one user turn
    assert [b["tool_use_id"] for b in results["content"]] == ["c1", "c2"]
    assert results["content"][1]["is_error"] is True
    assert reply.text == "Here you go" and reply.stop_reason == "end"
    assert (reply.usage.input_tokens, reply.usage.cached_input_tokens) == (100, 90)


def test_anthropic_tool_use_and_native_blocks_sent_back_unchanged() -> None:
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    use = SimpleNamespace(type="tool_use", id="t1", name="search_cars", input={"model": "creta"})
    llm, fake = claude(claude_response(thinking, use, stop="tool_use"))
    reply = llm.complete("S", [ChatMessage.user("creta?")], [SEARCH])
    assert reply.stop_reason == "tool_calls"
    assert reply.tool_calls == [ToolCall("t1", "search_cars", {"model": "creta"})]

    history = [ChatMessage.user("creta?"), reply.to_message(), ChatMessage.tool_result("t1", "[]")]
    llm.complete("S", history, [SEARCH])
    assert fake.kwargs["messages"][1]["content"] == [thinking, use]


def test_anthropic_refusal_and_extra_fields() -> None:
    messages = FakeMessages(claude_response(stop="refusal"))
    llm = AnthropicLLM("anthropic", model="m", api_key="k", extra={"output_config": {"effort": "low"}},
                       client=SimpleNamespace(messages=messages))
    assert llm.complete("S", [ChatMessage.user("x")]).stop_reason == "refusal"
    assert messages.kwargs["output_config"] == {"effort": "low"}


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (anthropic.InternalServerError("boom", response=httpx2.Response(500, request=_request()), body=None), True),
        (anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=_request()), body=None), False),
        (anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=_request()), body=None), True),
        (anthropic.APIConnectionError(request=_request()), True),
    ],
)
def test_anthropic_errors_become_llm_errors(error: Exception, retryable: bool) -> None:
    llm, _ = claude(error)
    with pytest.raises(LLMError) as info:
        llm.complete("S", [ChatMessage.user("x")])
    assert info.value.retryable is retryable


# --------------------------------------------------------------------------- fallback chain


def test_fallback_uses_next_provider_after_failure_or_refusal() -> None:
    down = MockLLM([LLMError("deepseek", "HTTP 503")], name="deepseek")
    refuses = MockLLM([LLMResponse("", stop_reason="refusal")], name="openrouter")
    works = MockLLM([MockLLM.text("hello")], name="anthropic")
    reply = FallbackLLM([down, refuses, works]).complete("S", [ChatMessage.user("hi")])
    assert (reply.text, reply.provider) == ("hello", "anthropic")
    assert len(down.calls) == len(refuses.calls) == len(works.calls) == 1


def test_fallback_stops_at_first_success() -> None:
    first, second = MockLLM([MockLLM.text("one")], name="a"), MockLLM([], name="b")
    assert FallbackLLM([first, second]).complete("S", []).text == "one"
    assert second.calls == []


def test_fallback_all_failed_or_all_refused() -> None:
    chain = FallbackLLM([MockLLM([LLMError("a", "down")], name="a"), MockLLM([LLMError("b", "down")], name="b")])
    with pytest.raises(LLMError, match=r"all providers failed: \[a\] down \| \[b\] down"):
        chain.complete("S", [])
    refused = FallbackLLM([MockLLM([LLMResponse("", stop_reason="refusal")])])
    assert refused.complete("S", []).stop_reason == "refusal"


# --------------------------------------------------------------------------- factory


class DictSecrets:
    def __init__(self, **values: str) -> None:
        self.values = values

    def get(self, name: str) -> str:
        if name not in self.values:
            raise SecretNotFound(f"Secret '{name}' not set")
        return self.values[name]


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch, clients_dir: Path):
    monkeypatch.setenv("ANTHROPIC_MODEL", "")
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-chat")
    return load_client_config("truechoice", clients_dir)


def test_build_llm_skips_providers_without_key_or_model(config) -> None:
    llm = build_llm(config, DictSecrets(deepseek_api_key="d", anthropic_api_key="a"))
    # openrouter has no key, anthropic has no model name -> only deepseek is left
    assert [p.name for p in llm.providers] == ["deepseek"]
    assert isinstance(llm.providers[0], OpenAICompatibleLLM)


def test_build_llm_with_full_chain(config, monkeypatch: pytest.MonkeyPatch, clients_dir: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "some-claude-model")
    config = load_client_config("truechoice", clients_dir)
    llm = build_llm(config, DictSecrets(deepseek_api_key="d", openrouter_api_key="o", anthropic_api_key="a"))
    assert [p.name for p in llm.providers] == ["deepseek", "openrouter", "anthropic"]
    assert llm.providers[2].model == "some-claude-model"


def test_build_llm_explains_when_nothing_is_usable(config) -> None:
    with pytest.raises(LLMNotConfigured) as info:
        build_llm(config, DictSecrets())
    message = str(info.value)
    assert "deepseek" in message and "anthropic: no model name set" in message


@respx.mock
def test_thinking_headroom_is_added_to_max_tokens() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}))
    OpenAICompatibleLLM("gemini", base_url="https://api.example.com", model="m", api_key="k",
                        thinking_headroom_tokens=2048).complete("S", [ChatMessage.user("hi")], max_tokens=300)
    assert json.loads(route.calls[0].request.content)["max_tokens"] == 2348


@respx.mock
def test_gemini_thought_signature_is_echoed_back() -> None:
    signature = {"google": {"thought_signature": "abc123"}}
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": "c1", "extra_content": signature,
             "function": {"name": "search_cars", "arguments": '{"model": "swift"}'}}]}}]}),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}),
    ])
    llm = openai_llm()
    first = llm.complete("S", [ChatMessage.user("swift?")], [SEARCH])
    assert first.tool_calls[0].extra_content == signature
    llm.complete("S", [ChatMessage.user("swift?"), first.to_message(), ChatMessage.tool_result("c1", "[]")], [SEARCH])
    sent = json.loads(route.calls[1].request.content)["messages"][2]["tool_calls"][0]
    assert sent["extra_content"] == signature
