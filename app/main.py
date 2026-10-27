"""FastAPI app factory: health check and the WhatsApp webhook (dashboard comes in step 12).

Run locally:  uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.runtime import Runtime
from app.scheduler import start_background_jobs
from app.settings import get_settings
from app.resort import web as resort_web
from app.whatsapp import webhook


def create_app(runtime: Runtime | None = None) -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines print full URLs (sheet id)
    runtime = runtime or Runtime.from_settings(settings)  # fails fast on bad client config

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = start_background_jobs(runtime, settings.alerts_every_minutes)  # off unless configured
        stop_resort = None
        if os.environ.get("RESORT_AUTO_SEED") == "1":  # hosts with a wiped disk start from the placeholder data
            from app.resort.seed import seed
            logging.getLogger("resort").info("Auto-seed: %s", " | ".join(
                line for line in seed(resort_web.get_services().repo) if "password" not in line))
        if os.environ.get("RESORT_WHATSAPP_PHONE_NUMBER_ID"):  # resort bot is configured: expire unpaid holds
            stop_resort = resort_web.start_sweeper(resort_web.get_services())
        yield
        stop()
        if stop_resort:
            stop_resort()

    app = FastAPI(title="Dealer WhatsApp Agent", lifespan=lifespan)
    app.state.runtime = runtime
    app.include_router(webhook.router)
    app.include_router(resort_web.router)  # /resort/webhook and /resort/razorpay/webhook
    try:  # the resort dashboard is built separately; the app still starts without it
        from app.resort.dashboard import router as dashboard_router
        app.include_router(dashboard_router)
    except ImportError:
        pass

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "env": settings.app_env, "clients": sorted(runtime.clients)}

    return app


app = create_app()
