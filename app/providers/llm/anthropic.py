"""Claude via the official Anthropic SDK (Messages API), with prompt caching.

The model ID comes only from config (ANTHROPIC_MODEL in .env); nothing is hardcoded.
The system prompt and tool list are identical on every turn, so one cache breakpoint
on the system block caches tools + system together.
"""

from __future__ import annotations

from typing import Any

import anthropic

from app.providers.llm.base import (
    ChatMessage,
    LLMError,
    LLMResponse,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
)

_STOP: dict[str, StopReason] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_calls",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def _assistant_content(m: ChatMessage) -> Any:
    native = m.provider_data.get("anthropic")
    if native is not None:  # Claude's own blocks (incl. thinking), sent back unchanged
        return native
    blocks: list[dict[str, Any]] = []
    if m.content:
        blocks.append({"type": "text", "text": m.content})
    for c in m.tool_calls:
        blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
    return blocks or m.content


def _to_wire(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Tool results become tool_result blocks; consecutive ones share one user turn."""
    wire: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            if m.is_error:
                block["is_error"] = True
            prev = wire[-1] if wire else None
            if prev and prev["role"] == "user" and isinstance(prev["content"], list) \
                    and prev["content"] and prev["content"][0].get("type") == "tool_result":
                prev["content"].append(block)
            else:
                wire.append({"role": "user", "content": [block]})
        elif m.role == "assistant":
            wire.append({"role": "assistant", "content": _assistant_content(m)})
        else:
            wire.append({"role": "user", "content": m.content})
    return wire


class AnthropicLLM:
    def __init__(
        self,
        name: str,
        *,
        model: str,
        api_key: str,
        timeout_s: float = 30.0,
        extra: dict[str, Any] | None = None,
        client: Any | None = None,
    ) -> None:
        self.name = name
        self.model = model
        # The fallback chain is our retry strategy, so keep SDK retries short.
        self._client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=1)
        # Optional request fields from client.yaml `extra`, e.g. {"output_config": {"effort": "low"}}.
        self._extra = dict(extra or {})

    def complete(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": _to_wire(messages),
            **self._extra,
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise LLMError(self.name, f"rate limited: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(self.name, f"HTTP {exc.status_code}: {exc.message}",
                           retryable=exc.status_code >= 500) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(self.name, f"connection failed: {exc!r}") from exc

        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                error = None if isinstance(block.input, dict) else "arguments must be a JSON object"
                calls.append(ToolCall(block.id, block.name, args, error))

        usage = response.usage
        return LLMResponse(
            text="".join(texts),
            tool_calls=calls,
            stop_reason=_STOP.get(response.stop_reason or "", "end"),
            provider=self.name,
            model=response.model,
            usage=Usage(
                usage.input_tokens + (usage.cache_creation_input_tokens or 0)
                + (usage.cache_read_input_tokens or 0),
                usage.output_tokens,
                usage.cache_read_input_tokens or 0,
            ),
            provider_data={"anthropic": list(response.content)},
        )
