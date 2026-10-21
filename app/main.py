"""FastAPI app factory: health check and the WhatsApp webhook (dashboard comes in step 12).

Run locally:  uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.runtime import Runtime
from app.scheduler import start_background_jobs
from app.settings import get_settings
from app.whatsapp import webhook


def create_app(runtime: Runtime | None = None) -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines print full URLs (sheet id)
    runtime = runtime or Runtime.from_settings(settings)  # fails fast on bad client config

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = start_background_jobs(runtime, settings.alerts_every_minutes)  # off unless configured
        yield
        stop()

    app = FastAPI(title="Dealer WhatsApp Agent", lifespan=lifespan)
    app.state.runtime = runtime
    app.include_router(webhook.router)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "env": settings.app_env, "clients": sorted(runtime.clients)}

    return app


app = create_app()
