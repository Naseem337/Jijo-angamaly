"""Resort management dashboard. Mount with `app.include_router(router)`; routes live under /resort/admin."""

from app.resort.dashboard.routes import router

__all__ = ["router"]
