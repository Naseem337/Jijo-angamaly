"""Any OpenAI-style /chat/completions endpoint: DeepSeek, OpenRouter, Gemini's compat API."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.providers.llm.base import (
    ChatMessage,
    LLMError,
    LLMResponse,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
)

_FINISH: dict[str, StopReason] = {
    "stop": "end",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _to_wire(system: str, messages: list[ChatMessage]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "tool":
            wire.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        elif m.role == "assistant" and m.tool_calls:
            wire.append({
                "role": "assistant",
                "content": m.content or None,
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                    for c in m.tool_calls
                ],
            })
        else:
            wire.append({"role": m.role, "content": m.content})
    return wire


def _parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    fn = raw.get("function") or {}
    text = fn.get("arguments") or "{}"
    try:
        args = json.loads(text) if isinstance(text, str) else text
    except json.JSONDecodeError as exc:
        return ToolCall(raw.get("id", ""), fn.get("name", ""), {}, f"arguments are not valid JSON: {exc}")
    if not isinstance(args, dict):
        return ToolCall(raw.get("id", ""), fn.get("name", ""), {}, "arguments must be a JSON object")
    return ToolCall(raw.get("id", ""), fn.get("name", ""), args)


class OpenAICompatibleLLM:
    def __init__(
        self,
        name: str,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: float = 30.0,
        thinking_headroom_tokens: int = 0,
        extra: dict[str, Any] | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._headroom = thinking_headroom_tokens
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._extra = dict(extra or {})  # e.g. {"temperature": 0.3}
        self._http = http or httpx.Client(timeout=timeout_s)

    def complete(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _to_wire(system, messages),
            "max_tokens": max_tokens + self._headroom,
            **self._extra,
        }
        if tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        try:
            resp = self._http.post(self._url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise LLMError(self.name, f"request failed: {exc!r}") from exc
        if resp.status_code != 200:
            retryable = resp.status_code == 429 or resp.status_code >= 500
            raise LLMError(self.name, f"HTTP {resp.status_code}: {resp.text[:300]}", retryable=retryable)

        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(self.name, f"unexpected response: {resp.text[:300]}") from exc

        calls = [_parse_tool_call(c) for c in message.get("tool_calls") or []]
        stop = _FINISH.get(choice.get("finish_reason") or "", "end")
        if calls:
            stop = "tool_calls"
        usage = data.get("usage") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") \
            or usage.get("prompt_cache_hit_tokens") or 0  # OpenAI-style / DeepSeek-style
        return LLMResponse(
            text=message.get("content") or "",
            tool_calls=calls,
            stop_reason=stop,
            provider=self.name,
            model=data.get("model") or self.model,
            usage=Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), cached),
        )
