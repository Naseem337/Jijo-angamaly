"""Shared fixtures. Tests never need real API keys or network access."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.core.clock import IST, FixedClock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLIENTS_DIR = PROJECT_ROOT / "clients"


@pytest.fixture(autouse=True)
def _ignore_local_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's .env may override the LLM chain or inventory; tests use committed defaults.
    (An empty value wins over .env, because load_dotenv never overwrites existing vars.)"""
    monkeypatch.setenv("LLM_CHAIN", "")
    monkeypatch.setenv("TRUECHOICE_INVENTORY_ADAPTER", "")  # tests use the mock stock


@pytest.fixture
def clock() -> FixedClock:
    """Thursday 16 Oct 2026, 10:00 IST."""
    return FixedClock(datetime(2026, 10, 16, 10, 0, tzinfo=IST))


@pytest.fixture
def clients_dir() -> Path:
    return CLIENTS_DIR
