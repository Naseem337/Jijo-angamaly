"""Builds the resort bot's services once per process: repo, WhatsApp client, payments, LLM, booking bot.

Env: RESORT_DB_PATH (default data/resort.sqlite3), RESORT_WHATSAPP_PHONE_NUMBER_ID,
RESORT_WHATSAPP_ACCESS_TOKEN, RAZORPAY_* (see payments.py). Anything missing is logged, not fatal:
the web app and dashboard still start, and the bot just can't send until the values are filled in.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path
from typing import Any

from app.core.clock import Clock, SystemClock
from app.providers.audio import find_ffmpeg  # noqa: F401  (STT splits long notes with it when present)
from app.providers.llm import LLMProvider, build_llm
from app.providers.stt import build_stt
from app.resort.config import ResortConfig, load_resort_config
from app.resort.db import ResortRepo
from app.resort.flow import BookingBot
from app.resort.payments import Payments, build_payments
from app.settings import PROJECT_ROOT, EnvSecretResolver, SecretNotFound, Settings, get_settings
from app.whatsapp.client import WhatsAppClient

log = logging.getLogger(__name__)


@dataclass
class ResortServices:
    config: ResortConfig
    repo: ResortRepo
    whatsapp: WhatsAppClient
    payments: Payments
    clock: Clock
    secrets: Any
    _llm: LLMProvider | None = None

    @cached_property
    def llm(self) -> LLMProvider:
        return self._llm or build_llm(self.config, self.secrets)  # type: ignore[arg-type]

    def _voice_in(self):
        from app.conversation.voice_in import VoiceIn
        try:
            return VoiceIn(self.whatsapp, build_stt(self.config, self.secrets),  # type: ignore[arg-type]
                           max_seconds=self.config.max_audio_seconds)
        except SecretNotFound as exc:
            log.warning("Voice notes disabled for the resort bot: %s", exc)
            return None

    @cached_property
    def bot(self) -> BookingBot:
        return BookingBot(self.config, self.repo, self.whatsapp, self.payments, self.clock,
                          llm=lambda: self.llm, voice_in=self._voice_in)


def build_resort_services(settings: Settings | None = None) -> ResortServices:
    settings = settings or get_settings()
    secrets = EnvSecretResolver()  # also loads .env
    config = load_resort_config()
    db_path = Path(os.environ.get("RESORT_DB_PATH") or PROJECT_ROOT / "data" / "resort.sqlite3")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    phone_id = os.environ.get("RESORT_WHATSAPP_PHONE_NUMBER_ID", "")
    token = os.environ.get("RESORT_WHATSAPP_ACCESS_TOKEN", "")
    if not (phone_id and token):
        log.warning("RESORT_WHATSAPP_PHONE_NUMBER_ID / RESORT_WHATSAPP_ACCESS_TOKEN not set: the resort bot can't send")
    return ResortServices(config, ResortRepo(db_path), WhatsAppClient(phone_id or "not-set", token or "not-set",
                                                                       api_version="v25.0"),
                          build_payments(), SystemClock(), secrets)


@cache
def get_services() -> ResortServices:
    """The process-wide instance (the dashboard uses this too, so bot and dashboard share one database)."""
    return build_resort_services()
