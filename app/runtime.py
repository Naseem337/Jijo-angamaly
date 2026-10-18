"""Builds and caches each client's services (storage, LLM, agent, WhatsApp, router).

One Runtime per process. Services are built on first use, so the web app starts even
if a key is missing; the error then appears in the log for the request that needs it.
"""

from __future__ import annotations

import logging
from functools import cached_property

import httpx

from app.agent import Agent
from app.conversation.router import MessageRouter
from app.conversation.voice_in import VoiceIn
from app.conversation.voice_out import VoiceOut
from app.core.client_config import ClientConfig, load_all_clients
from app.core.clock import Clock, SystemClock
from app.inventory import InventoryAdapter, build_inventory_adapter
from app.providers.llm import LLMProvider, build_llm
from app.providers.audio import find_ffmpeg
from app.providers.stt import STTProvider, build_stt
from app.providers.tts import TTSProvider, build_tts
from app.settings import EnvSecretResolver, SecretNotFound, SecretResolver, Settings, get_settings
from app.storage.repository import Repository
from app.whatsapp.client import WhatsAppClient

log = logging.getLogger(__name__)


def build_repository(settings: Settings) -> Repository:
    if settings.storage_backend == "sqlite":
        from app.storage.sqlite_repo import SQLiteRepository

        settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return SQLiteRepository(settings.sqlite_path)
    raise NotImplementedError("DynamoDB storage arrives in step 11")


class ClientRuntime:
    def __init__(self, config: ClientConfig, repo: Repository, secrets: SecretResolver, clock: Clock,
                 *, llm: LLMProvider | None = None, stt: STTProvider | None = None,
                 tts: TTSProvider | None = None, ffmpeg: str | None = None,
                 http: httpx.Client | None = None) -> None:
        self.config = config
        self.repo = repo
        self.secrets = secrets
        self.clock = clock
        self._llm = llm
        self._stt = stt
        self._tts = tts
        self._ffmpeg = ffmpeg
        self._http = http

    def secret(self, name: str) -> str:
        return self.secrets.get(name)

    @cached_property
    def inventory(self) -> InventoryAdapter:
        return build_inventory_adapter(self.config)

    @cached_property
    def agent(self) -> Agent:
        llm = self._llm or build_llm(self.config, self.secrets)
        return Agent(self.config, llm, self.repo, self.inventory, self.clock)

    @cached_property
    def whatsapp(self) -> WhatsAppClient:
        wa = self.config.whatsapp
        return WhatsAppClient(wa.phone_number_id, self.secret(wa.access_token_secret),
                              api_version=wa.graph_api_version, http=self._http)

    @cached_property
    def voice_in(self) -> VoiceIn | None:
        try:
            stt = self._stt or build_stt(self.config, self.secrets)
        except SecretNotFound as exc:
            log.warning("Voice notes disabled for %s: %s", self.config.client_id, exc)
            return None
        return VoiceIn(self.whatsapp, stt, max_seconds=self.config.voice.max_audio_seconds)

    @cached_property
    def voice_out(self) -> VoiceOut | None:
        if self.config.voice.policy == "never":
            return None
        ffmpeg = self._ffmpeg or find_ffmpeg()
        if ffmpeg is None:
            log.warning("Voice replies disabled for %s: ffmpeg not found", self.config.client_id)
            return None
        try:
            tts = self._tts or build_tts(self.config, self.secrets)
        except SecretNotFound as exc:
            log.warning("Voice replies disabled for %s: %s", self.config.client_id, exc)
            return None
        return VoiceOut(self.config, tts, self.whatsapp, self.repo, self.clock, ffmpeg)

    @cached_property
    def router(self) -> MessageRouter:
        return MessageRouter(self.config, self.repo, lambda: self.agent, self.whatsapp, self.clock,
                             lambda: self.voice_in, lambda: self.voice_out)


class Runtime:
    def __init__(self, clients: dict[str, ClientConfig], repo: Repository, secrets: SecretResolver,
                 clock: Clock | None = None, *, llm: LLMProvider | None = None,
                 stt: STTProvider | None = None, tts: TTSProvider | None = None,
                 ffmpeg: str | None = None, http: httpx.Client | None = None) -> None:
        self.clients = {
            cid: ClientRuntime(cfg, repo, secrets, clock or SystemClock(), llm=llm, stt=stt, tts=tts,
                               ffmpeg=ffmpeg, http=http)
            for cid, cfg in clients.items()
        }

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "Runtime":
        settings = settings or get_settings()
        return cls(load_all_clients(settings.clients_dir), build_repository(settings), EnvSecretResolver())

    def get(self, client_id: str) -> ClientRuntime | None:
        return self.clients.get(client_id)
