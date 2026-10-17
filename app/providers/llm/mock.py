"""Scripted LLM for tests and offline development. Records every call it receives."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.providers.llm.base import ChatMessage, LLMError, LLMResponse, ToolCall, ToolSpec

Script = LLMResponse | LLMError | Callable[[str, list[ChatMessage], list[ToolSpec]], LLMResponse]


@dataclass
class RecordedCall:
    system: str
    messages: list[ChatMessage]
    tools: list[ToolSpec]
    max_tokens: int


class MockLLM:
    """Returns (or raises) the scripted items in order. An exhausted script is an error,
    so a test never silently gets a reply it didn't plan for."""

    def __init__(self, script: list[Script] | None = None, name: str = "mock") -> None:
        self.name = name
        self.script: list[Script] = list(script or [])
        self.calls: list[RecordedCall] = []

    @staticmethod
    def text(text: str) -> LLMResponse:
        return LLMResponse(text, provider="mock", model="mock")

    @staticmethod
    def tool(name: str, /, call_id: str = "call_1", text: str = "", **arguments: Any) -> LLMResponse:
        """A reply that calls tool `name`; keyword arguments become the tool's arguments."""
        return LLMResponse(text, [ToolCall(call_id, name, arguments)], "tool_calls",
                           provider="mock", model="mock")

    def complete(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        self.calls.append(RecordedCall(system, list(messages), list(tools or []), max_tokens))
        if not self.script:
            raise AssertionError(f"MockLLM '{self.name}' has no scripted reply left")
        item = self.script.pop(0)
        if isinstance(item, LLMError):
            raise item
        if callable(item):
            item = item(system, messages, list(tools or []))
        item.provider = self.name
        return item
