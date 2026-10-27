"""Resort management dashboard. Mount with `app.include_router(router)`; the pages live under /resort/admin and
the signed guest invoice link under /resort/invoice."""

from fastapi import APIRouter

from app.resort.dashboard.routes import public_router, router as _admin_router

router = APIRouter()
router.include_router(_admin_router)
router.include_router(public_router)

__all__ = ["router"]
