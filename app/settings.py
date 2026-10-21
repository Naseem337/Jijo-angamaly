"""Process-wide settings (from env / .env) and secret resolution.

Client-specific settings live in clients/<id>/client.yaml (see core/client_config.py).
This module only holds things that are the same for every client in one deployment.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Deployment-level settings. Values come from env vars or a local .env file."""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    app_env: Literal["local", "test", "aws", "railway"] = "local"
    log_level: str = "INFO"
    clients_dir: Path = PROJECT_ROOT / "clients"
    storage_backend: Literal["sqlite", "dynamodb"] = "sqlite"
    sqlite_path: Path = PROJECT_ROOT / "data" / "app.sqlite3"
    session_secret: str = "change-me"
    # Alert runs + auto-resume sweep inside the web process (Railway). 0 = off (local: run by hand).
    alerts_every_minutes: int = 0
    aws_region: str = "ap-south-1"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings instance (call get_settings.cache_clear() in tests)."""
    return Settings()


# --------------------------------------------------------------------------- secrets


class SecretNotFound(KeyError):
    """Raised when a secret named in config cannot be resolved."""


class SecretResolver(Protocol):
    """Turns a secret *name* from client.yaml into its value."""

    def get(self, name: str) -> str: ...


def secret_name_to_env_var(name: str) -> str:
    """'truechoice/whatsapp_token' -> 'TRUECHOICE_WHATSAPP_TOKEN'."""
    return re.sub(r"[^A-Za-z0-9]", "_", name).upper()


class EnvSecretResolver:
    """Local/dev resolver: reads secrets from environment variables (.env is loaded first)."""

    def __init__(self, read_dotenv_file: bool = True) -> None:
        if read_dotenv_file:
            load_dotenv()

    def get(self, name: str) -> str:
        var = secret_name_to_env_var(name)
        value = os.environ.get(var, "")
        if not value:
            raise SecretNotFound(f"Secret '{name}' not set (expected env var {var})")
        return value


def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Real env vars always win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
