"""Provider-neutral chat types and the LLMProvider interface.

The agent speaks only these types. Each provider converts them to its own wire format
(OpenAI-style chat completions for DeepSeek/OpenRouter/Gemini, the Messages API for
Claude), so providers can be swapped or chained without touching the agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["user", "assistant", "tool"]
StopReason = Literal["end", "tool_calls", "max_tokens", "refusal"]


class LLMError(RuntimeError):
    """A provider call failed. The fallback chain moves on to the next provider."""

    def __init__(self, provider: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retryable = retryable


class LLMNotConfigured(RuntimeError):
    """No provider in the chain has the model name and API key it needs."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments object


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Set when the model produced arguments that are not a JSON object. The agent
    # reports it back to the model as a tool error instead of running the tool.
    arguments_error: str | None = None


@dataclass
class ChatMessage:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant only
    tool_call_id: str | None = None  # tool only: which call this result answers
    is_error: bool = False  # tool only
    # Provider-native form of an assistant turn, keyed by provider name. Claude needs
    # its own content blocks (including thinking) sent back unchanged inside a tool
    # loop; other providers ignore this.
    provider_data: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def user(cls, text: str) -> "ChatMessage":
        return cls("user", text)

    @classmethod
    def tool_result(cls, call_id: str, content: str, *, is_error: bool = False) -> "ChatMessage":
        return cls("tool", content, tool_call_id=call_id, is_error=is_error)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason = "end"
    provider: str = ""
    model: str = ""
    usage: Usage = Usage()
    provider_data: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_message(self) -> ChatMessage:
        """The assistant turn to append to the conversation before sending tool results."""
        return ChatMessage("assistant", self.text, list(self.tool_calls),
                           provider_data=dict(self.provider_data))


class LLMProvider(Protocol):
    name: str

    def complete(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        """One model call. Raises LLMError on any failure (network, HTTP, bad payload)."""
        ...
