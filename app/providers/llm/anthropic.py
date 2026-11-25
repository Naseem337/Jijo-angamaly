"""Claude via the official Anthropic SDK (Messages API), with prompt caching.

The model ID comes only from config (ANTHROPIC_MODEL for the car bot, AGENT_MODEL for the resort agent); nothing is
hardcoded. The API key is read by the provider factory from the secret store and never logged.
Prompt caching: one breakpoint after the tools and one after the static system part (see `request`).
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
    split_system,
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


CACHE = {"type": "ephemeral"}


def request(model: str, system: str, messages: list[ChatMessage], tools: list[ToolSpec] | None, max_tokens: int,
            temperature: float | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The Messages API request. Claude reads tools -> system -> messages, so the order of the cached prefix is:
    tool definitions [breakpoint 1: shared by every guest] -> the static system part [breakpoint 2] -> the dynamic
    system part (after CACHE_SPLIT: changes every turn, never cached) -> the chat.

    `temperature` is never sent: the API version this SDK uses (2026-10-21) rejects sampling parameters on every model;
    the other providers in the chain still get it. Thinking is set per client in client.yaml `extra` (the resort agent
    sends thinking disabled: no extended thinking)."""
    static, dynamic = split_system(system)
    blocks: list[dict[str, Any]] = [{"type": "text", "text": static, "cache_control": CACHE}]
    if dynamic.strip():
        blocks.append({"type": "text", "text": dynamic})
    kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": blocks,
                              "messages": _to_wire(messages), **(extra or {})}
    if tools:
        kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        kwargs["tools"][-1]["cache_control"] = CACHE
    return kwargs


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
        temperature: float | None = None,
    ) -> LLMResponse:
        kwargs = request(self.model, system, messages, tools, max_tokens, temperature, self._extra)
        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise LLMError(self.name, f"rate limited: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(self.name, f"HTTP {exc.status_code}: {exc.message}",
                           retryable=exc.status_code >= 500) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(self.name, f"connection failed: {type(exc).__name__}") from exc
        except Exception as exc:  # anything else (a bad request field, an SDK change): the next provider answers
            raise LLMError(self.name, f"request failed: {type(exc).__name__}: {str(exc)[:200]}",
                           retryable=False) from exc

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
                usage.cache_creation_input_tokens or 0,
            ),
            provider_data={"anthropic": list(response.content)},
        )
