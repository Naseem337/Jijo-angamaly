"""Try each provider in order (DeepSeek → OpenRouter → Anthropic by default)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from app.providers.llm.base import ChatMessage, LLMError, LLMProvider, LLMResponse, ToolSpec

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 120.0
# Free tiers (Groq: 8,000 tokens/min per model) say "try again in 11.5s". Waiting that long once
# beats sending the customer an apology (live 2026-10-21).
MAX_RATE_LIMIT_WAIT_S = 15.0


class FallbackLLM:
    """A provider made of other providers. A failure or a refusal moves on to the next
    one; the first usable answer wins. Raises LLMError only if every provider fails.

    A provider that just failed with a retryable error (timeout, overload, rate limit) is
    skipped for COOLDOWN_SECONDS. One reply takes 2–4 calls; without this, a slow model
    made the customer wait for its timeout on every call (over a minute, live 2026-10-20).
    If every provider is cooling down, they are all tried anyway rather than giving up.
    A rate limit that names its reset time ("try again in 11s") rests the provider only that
    long, and if every provider is rate-limited for at most MAX_RATE_LIMIT_WAIT_S, the chain
    waits once and tries again.
    """

    name = "fallback"

    def __init__(self, providers: list[LLMProvider], *, cooldown_s: float = COOLDOWN_SECONDS,
                 max_wait_s: float = MAX_RATE_LIMIT_WAIT_S,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not providers:
            raise ValueError("FallbackLLM needs at least one provider")
        self.providers = providers
        self._cooldown_s = cooldown_s
        self._max_wait_s = max_wait_s
        self._monotonic = monotonic
        self._sleep = sleep
        self._resting_until: dict[str, float] = {}

    def _order(self) -> list[LLMProvider]:
        now = self._monotonic()
        ready = [p for p in self.providers if self._resting_until.get(p.name, 0) <= now]
        resting = [p for p in self.providers if p not in ready]
        return ready + resting  # resting ones only get a turn if every ready one fails

    def complete(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        failures: list[str] = []
        refusal: LLMResponse | None = None
        for attempt in range(2):
            waits: list[float] = []
            for provider in self._order():
                try:
                    response = provider.complete(system, messages, tools, max_tokens)
                except LLMError as exc:
                    log.warning("LLM provider failed, trying next: %s", exc)
                    failures.append(str(exc))
                    if exc.retryable:
                        rest = exc.retry_after if exc.retry_after is not None else self._cooldown_s
                        self._resting_until[provider.name] = self._monotonic() + rest
                    if exc.retry_after is not None:
                        waits.append(exc.retry_after)
                    continue
                self._resting_until.pop(provider.name, None)
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
            wait = min(waits, default=None)
            if attempt or wait is None or wait > self._max_wait_s:
                break
            log.info("Every provider is rate-limited; waiting %.1f s and trying once more", wait)
            self._sleep(wait + 0.5)
        raise LLMError(self.name, "all providers failed: " + " | ".join(failures))
