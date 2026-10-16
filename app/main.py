"""FastAPI app factory. Routers (webhook, dashboard) are mounted here in later steps."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.core.client_config import load_all_clients
from app.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    clients = load_all_clients(settings.clients_dir)  # fail fast on bad config

    app = FastAPI(title="Dealer WhatsApp Agent")
    app.state.clients = clients

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "env": settings.app_env, "clients": sorted(clients)}

    return app


app = create_app()
