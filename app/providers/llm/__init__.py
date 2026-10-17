"""LLM providers. build_llm() turns a client's `providers.llm` config into one provider."""

from __future__ import annotations

import logging

from app.core.client_config import ClientConfig, LLMProviderConfig
from app.providers.llm.anthropic import AnthropicLLM
from app.providers.llm.base import (
    ChatMessage,
    LLMError,
    LLMNotConfigured,
    LLMProvider,
    LLMResponse,
    ToolCall,
    ToolSpec,
    Usage,
)
from app.providers.llm.fallback import FallbackLLM
from app.providers.llm.mock import MockLLM
from app.providers.llm.openai_compatible import OpenAICompatibleLLM
from app.settings import SecretNotFound, SecretResolver

__all__ = [
    "AnthropicLLM", "ChatMessage", "FallbackLLM", "LLMError", "LLMNotConfigured", "LLMProvider",
    "LLMResponse", "MockLLM", "OpenAICompatibleLLM", "ToolCall", "ToolSpec", "Usage", "build_llm",
]

log = logging.getLogger(__name__)


def _build_one(name: str, cfg: LLMProviderConfig, secrets: SecretResolver) -> LLMProvider:
    """Raises LLMNotConfigured with the reason if the provider can't be used."""
    if cfg.kind == "mock":
        return MockLLM(name=name)
    if not cfg.model:
        raise LLMNotConfigured(f"{name}: no model name set")
    if not cfg.api_key_secret:
        raise LLMNotConfigured(f"{name}: no api_key_secret in client.yaml")
    try:
        api_key = secrets.get(cfg.api_key_secret)
    except SecretNotFound as exc:
        raise LLMNotConfigured(f"{name}: {exc.args[0]}") from exc
    if cfg.kind == "anthropic":
        return AnthropicLLM(name, model=cfg.model, api_key=api_key, timeout_s=cfg.timeout_s, extra=cfg.extra)
    if not cfg.base_url:
        raise LLMNotConfigured(f"{name}: no base_url set")
    return OpenAICompatibleLLM(name, base_url=cfg.base_url, model=cfg.model, api_key=api_key,
                               timeout_s=cfg.timeout_s,
                               thinking_headroom_tokens=cfg.thinking_headroom_tokens, extra=cfg.extra)


def build_llm(config: ClientConfig, secrets: SecretResolver) -> FallbackLLM:
    """The client's llm_chain as one FallbackLLM. Providers missing a key or model are
    skipped with a warning; if none is usable, raises LLMNotConfigured listing why."""
    providers: list[LLMProvider] = []
    skipped: list[str] = []
    for name in config.providers.llm_chain:
        try:
            providers.append(_build_one(name, config.providers.llm[name], secrets))
        except LLMNotConfigured as exc:
            skipped.append(str(exc))
            log.warning("LLM provider skipped: %s", exc)
    if not providers:
        raise LLMNotConfigured(
            f"no usable LLM provider for client '{config.client_id}':\n  " + "\n  ".join(skipped)
        )
    return FallbackLLM(providers)
