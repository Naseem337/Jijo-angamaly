"""FastAPI app factory: health check and the WhatsApp webhook (dashboard comes in step 12).

Run locally:  uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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

    if os.environ.get("RESORT_WHATSAPP_PHONE_NUMBER_ID"):  # the resort service: the plain address opens the dashboard
        @app.get("/", include_in_schema=False)
        @app.get("/resort", include_in_schema=False)
        @app.get("/resort/", include_in_schema=False)
        def resort_home() -> RedirectResponse:
            return RedirectResponse("/resort/admin/")

    @app.exception_handler(StarletteHTTPException)
    async def friendly_404(request: Request, exc: StarletteHTTPException):
        """A browser that opens a dead dashboard page (an old link after the free host reset its data) gets a page
        with a way back, not raw JSON. Webhooks and API calls keep the JSON error."""
        if (exc.status_code == 404 and request.url.path.startswith("/resort/admin")
                and "text/html" in request.headers.get("accept", "")):
            return HTMLResponse(
                '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                "<title>Page not found</title><body style=\"font:18px/1.6 system-ui,sans-serif;max-width:480px;margin:18vh auto;"
                'padding:0 20px;color:#1f2937"><h1 style="font-size:28px">This page is not available</h1>'
                "<p>The booking, guest or room in this link may have been removed or the page moved.</p>"
                '<p><a style="display:inline-block;background:#2490ef;color:#fff;padding:12px 20px;border-radius:8px;'
                'text-decoration:none;font-weight:600" href="/resort/admin/">Go to the dashboard</a></p></body>',
                status_code=404)
        return await http_exception_handler(request, exc)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "env": settings.app_env, "clients": sorted(runtime.clients)}

    return app


app = create_app()
