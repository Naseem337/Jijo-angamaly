"""Per-client configuration: clients/<client_id>/client.yaml + business_rules.yaml.

YAML values may reference environment variables as ${VAR} or ${VAR:-default}, so
deployment-specific values (phone_number_id, staff numbers, the Claude model ID) stay
out of git. Secrets are never put in YAML. YAML only holds secret *names*, which
app.settings.SecretResolver turns into values.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.clock import parse_hhmm
from app.core.phone import normalize_phone
from app.settings import load_dotenv


class ConfigError(ValueError):
    """Client config is missing or invalid."""


# --------------------------------------------------------------------------- business rules


class BusinessHours(BaseModel):
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]]
    open: str = "09:30"
    close: str = "19:00"

    @field_validator("open", "close")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parse_hhmm(v)
        return v


class EmiRules(BaseModel):
    annual_rate_min_pct: float = 10.5
    annual_rate_max_pct: float = 14.0
    tenure_months_options: list[int] = [12, 24, 36, 48, 60]
    default_down_payment_pct: float = 20.0
    disclaimer_en: str
    disclaimer_ml: str

    @model_validator(mode="after")
    def _range(self) -> "EmiRules":
        if not 0 < self.annual_rate_min_pct <= self.annual_rate_max_pct:
            raise ValueError("EMI rate range is invalid")
        return self


class SendWindow(BaseModel):
    start: str = "09:00"
    end: str = "20:00"


class NearMatchRules(BaseModel):
    budget_over_pct: float = 10.0
    years_older: int = 1
    max_per_days: int = 7


class AlertRules(BaseModel):
    send_window: SendWindow = SendWindow()
    global_daily_cap: int = 200
    max_cars_per_message: int = Field(3, ge=1, le=3)
    max_unanswered: int = 3
    requirement_ttl_days: int = 60
    reply_context_hours: int = 72
    near_match: NearMatchRules = NearMatchRules()


class BotRules(BaseModel):
    auto_resume_hours: int = 24
    history_messages: int = 20


class BusinessRules(BaseModel):
    business_hours: BusinessHours
    emi: EmiRules
    alerts: AlertRules = AlertRules()
    bot: BotRules = BotRules()
    retention_days: int = 90
    opt_out_extra_phrases: list[str] = []
    # canonical model name -> spellings customers use ("xuv 7oo", "crysta", …)
    model_aliases: dict[str, list[str]] = {}


# --------------------------------------------------------------------------- client config


class LLMProviderConfig(BaseModel):
    kind: Literal["openai_compatible", "anthropic", "mock"]
    base_url: str | None = None
    # May be empty in YAML (e.g. ${ANTHROPIC_MODEL} not filled yet). The provider factory
    # skips an unconfigured provider with a warning instead of failing at startup.
    model: str | None = None
    api_key_secret: str | None = None
    timeout_s: float = 30.0
    # Some reasoning models (e.g. Gemini 3.x) count hidden thinking against max_tokens,
    # which cuts the visible reply short. This many tokens are added on top for them.
    thinking_headroom_tokens: int = Field(0, ge=0)
    extra: dict[str, Any] = {}

    @field_validator("model", "base_url", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        return v or None


class SpeechProviderConfig(BaseModel):
    kind: Literal["sarvam", "mock"]
    api_key_secret: str | None = None
    model: str | None = None
    language_code: str = "ml-IN"
    speaker: str | None = None  # TTS voice
    timeout_s: float = 20.0
    keyterms: list[str] = []  # STT spelling hints (car names), at most 50


class ProvidersConfig(BaseModel):
    llm: dict[str, LLMProviderConfig]
    llm_chain: list[str]

    @field_validator("llm_chain", mode="before")
    @classmethod
    def _split_chain(cls, v: Any) -> Any:
        """Accept a YAML list or a comma-separated string (so .env can override it)."""
        if isinstance(v, str):
            return [name.strip() for name in v.split(",") if name.strip()]
        return v
    stt: SpeechProviderConfig
    tts: SpeechProviderConfig

    @model_validator(mode="after")
    def _chain_known(self) -> "ProvidersConfig":
        unknown = [name for name in self.llm_chain if name not in self.llm]
        if unknown:
            raise ValueError(f"llm_chain refers to undefined providers: {unknown}")
        if not self.llm_chain:
            raise ValueError("llm_chain must list at least one provider")
        return self


class VoiceConfig(BaseModel):
    policy: Literal["mirror", "always", "never"] = "mirror"
    max_audio_seconds: int = 60
    tts_timeout_s: float = 8.0
    daily_char_cap: int = 20_000


class WhatsAppConfig(BaseModel):
    phone_number_id: str
    graph_api_version: str = "v25.0"
    access_token_secret: str
    app_secret_secret: str
    verify_token_secret: str
    # logical template -> approved template name in Meta
    templates: dict[str, str]
    # customer language -> template language code
    template_languages: dict[str, str] = {"ml": "ml", "en": "en", "manglish": "en"}


class InventoryConfig(BaseModel):
    adapter: Literal["mock", "google_sheet", "real"]
    options: dict[str, Any] = {}


class DashboardConfig(BaseModel):
    password_secret: str


class ClientConfig(BaseModel):
    client_id: str = Field(pattern=r"^[a-z0-9_]{2,32}$")
    display_name: str
    assistant_name: str
    languages: list[Literal["ml", "en", "manglish"]]
    staff_numbers: list[str] = []
    whatsapp: WhatsAppConfig
    inventory: InventoryConfig
    providers: ProvidersConfig
    voice: VoiceConfig = VoiceConfig()
    dashboard: DashboardConfig
    business_rules: BusinessRules
    config_dir: Path

    @field_validator("staff_numbers", mode="before")
    @classmethod
    def _split_and_normalise(cls, v: Any) -> list[str]:
        """Accept a YAML list or a comma-separated string (handy from ${ENV})."""
        if v is None or v == "":
            return []
        items = v.split(",") if isinstance(v, str) else list(v)
        return [normalize_phone(str(x)) for x in items if str(x).strip()]

    def is_staff(self, e164: str) -> bool:
        return e164 in self.staff_numbers


# --------------------------------------------------------------------------- loading

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _interpolate(value: Any) -> Any:
    """Recursively replace ${VAR} / ${VAR:-default} in strings with env values."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return _interpolate(data)


def load_client_config(client_id: str, clients_dir: Path) -> ClientConfig:
    """Load and validate one client's config. Raises ConfigError with a readable message."""
    load_dotenv()  # so ${VAR} references see .env values locally (real env vars win)
    folder = clients_dir / client_id
    raw = _read_yaml(folder / "client.yaml")
    raw["business_rules"] = _read_yaml(folder / "business_rules.yaml")
    raw["config_dir"] = folder
    if raw.get("client_id") != client_id:
        raise ConfigError(f"{folder / 'client.yaml'}: client_id must be '{client_id}'")
    try:
        return ClientConfig.model_validate(raw)
    except ValueError as exc:  # pydantic ValidationError and phone errors
        raise ConfigError(f"invalid config for client '{client_id}':\n{exc}") from exc


def load_all_clients(clients_dir: Path) -> dict[str, ClientConfig]:
    """Every folder under clients/ that has a client.yaml."""
    return {
        p.name: load_client_config(p.name, clients_dir)
        for p in sorted(clients_dir.iterdir())
        if (p / "client.yaml").exists()
    }


@lru_cache(maxsize=None)
def get_client_config(client_id: str, clients_dir: str) -> ClientConfig:
    """Cached variant for request handlers (clients_dir as str so it is hashable)."""
    return load_client_config(client_id, Path(clients_dir))
