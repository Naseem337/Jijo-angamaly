"""Try each provider in order (DeepSeek → OpenRouter → Anthropic by default)."""

from __future__ import annotations

import logging

from app.providers.llm.base import ChatMessage, LLMError, LLMProvider, LLMResponse, ToolSpec

log = logging.getLogger(__name__)


class FallbackLLM:
    """A provider made of other providers. A failure or a refusal moves on to the next
    one; the first usable answer wins. Raises LLMError only if every provider fails."""

    name = "fallback"

    def __init__(self, providers: list[LLMProvider]) -> None:
        if not providers:
            raise ValueError("FallbackLLM needs at least one provider")
        self.providers = providers

    def complete(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        failures: list[str] = []
        refusal: LLMResponse | None = None
        for provider in self.providers:
            try:
                response = provider.complete(system, messages, tools, max_tokens)
            except LLMError as exc:
                log.warning("LLM provider failed, trying next: %s", exc)
                failures.append(str(exc))
                continue
            if response.stop_reason == "refusal":
                log.warning("LLM provider %s refused, trying next", provider.name)
                failures.append(f"[{provider.name}] refused")
                refusal = response
                continue
            if failures:
                log.info("LLM answered by fallback provider %s", provider.name)
            return response
        if refusal is not None:
            return refusal  # every provider declined; the agent sends its safe reply
        raise LLMError(self.name, "all providers failed: " + " | ".join(failures))
