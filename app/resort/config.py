"""Resort bot config: clients/resort/resort.yaml (own loader, so the car bots never see it)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator

from app.core.client_config import ConfigError, ProvidersConfig, _interpolate
from app.core.phone import try_normalize_phone
from app.settings import PROJECT_ROOT, load_dotenv

DEFAULT_PATH = PROJECT_ROOT / "clients" / "resort" / "resort.yaml"


class ResortTemplates(BaseModel):
    paid: str = ""
    failed: str = ""
    pending: str = ""


class ResortConfig(BaseModel):
    client_id: str = "resort"
    display_name: str = "Resort Booking Assistant"
    bot_name: str = "Resort Assistant"
    languages: list[str] = ["en", "ml", "ta", "hi"]
    hold_minutes: int = 15
    max_audio_seconds: int = 60
    staff_numbers: list[str] = []
    templates: ResortTemplates = ResortTemplates()
    speaking_style: str = ""
    providers: ProvidersConfig

    @field_validator("staff_numbers", mode="before")
    @classmethod
    def _split(cls, v: Any) -> list[str]:
        if not v:
            return []
        items = v.split(",") if isinstance(v, str) else list(v)
        return [p for x in items if str(x).strip() and (p := try_normalize_phone(str(x).strip()))]


def load_resort_config(path: Path = DEFAULT_PATH) -> ResortConfig:
    load_dotenv()
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    raw = _interpolate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    try:
        return ResortConfig.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid resort config:\n{exc}") from exc
