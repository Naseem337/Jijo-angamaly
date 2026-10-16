"""Shared fixtures. Tests never need real API keys or network access."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.core.clock import IST, FixedClock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLIENTS_DIR = PROJECT_ROOT / "clients"


@pytest.fixture
def clock() -> FixedClock:
    """Thursday 16 Oct 2026, 10:00 IST."""
    return FixedClock(datetime(2026, 10, 16, 10, 0, tzinfo=IST))


@pytest.fixture
def clients_dir() -> Path:
    return CLIENTS_DIR
