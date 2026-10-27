"""Shared pieces of the dashboard: repo singleton, signed-cookie sessions, CSRF, templates, access rules."""

from __future__ import annotations

import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.core.clock import IST
from app.resort.db import ResortRepo
from app.resort.paying import DESK_METHODS, METHODS, method_label
from app.resort.models import DashboardUser, Property
from app.settings import PROJECT_ROOT, get_settings

BASE = "/resort/admin"
HERE = Path(__file__).parent
COOKIE = "resort_session"
SESSION_SECONDS = 60 * 60 * 24 * 7

_repos: dict[str, ResortRepo] = {}
_lock = threading.Lock()


def get_repo() -> ResortRepo:
    """One ResortRepo per database path (RESORT_DB_PATH, default data/resort.sqlite3)."""
    path = os.environ.get("RESORT_DB_PATH") or str(PROJECT_ROOT / "data" / "resort.sqlite3")
    with _lock:
        if path not in _repos:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            _repos[path] = ResortRepo(path)
        return _repos[path]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="resort-dashboard")


def make_session(email: str) -> str:
    return _serializer().dumps({"email": email, "csrf": secrets.token_urlsafe(16)})


def read_session(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        return _serializer().loads(raw, max_age=SESSION_SECONDS)
    except BadSignature:
        return None


class Auth:
    """The logged-in user plus the CSRF token of this session."""

    def __init__(self, user: DashboardUser, csrf: str) -> None:
        self.user = user
        self.csrf = csrf

    @property
    def is_owner(self) -> bool:
        return self.user.role == "owner"

    def can_see(self, property_id: str) -> bool:
        return not self.user.property_ids or property_id in self.user.property_ids

    def properties(self, repo: ResortRepo, active_only: bool = False) -> list[Property]:
        return [p for p in repo.list_properties(active_only) if self.can_see(p.id)]


def redirect_to(url: str, status: int = 303) -> HTTPException:
    return HTTPException(status, headers={"Location": url})


def require_user(request: Request) -> Auth:
    session = read_session(request)
    user = get_repo().get_user(session["email"]) if session else None
    if not session or not user or not user.active:
        raise redirect_to(f"{BASE}/login")
    return Auth(user, session["csrf"])


def require_owner(request: Request) -> Auth:
    auth = require_user(request)
    if not auth.is_owner:
        raise HTTPException(403, "Owner only")
    return auth


async def form_of(request: Request, auth: Auth) -> dict[str, str]:
    """Parsed POST form after the CSRF check. Repeated fields keep the last value; use form_list for those."""
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), auth.csrf):
        raise HTTPException(403, "Bad CSRF token")
    return {k: str(v) for k, v in form.items()}


def toast_url(url: str, message: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}toast={quote(message)}"


templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.globals["BASE"] = BASE
templates.env.filters["rupees"] = lambda n: f"₹{int(n or 0):,}"
templates.env.filters["urlq"] = lambda s: quote(str(s), safe="")


def ist(value: datetime | None, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    """A stored UTC time shown in Indian time."""
    return value.astimezone(IST).strftime(fmt).lstrip("0") + " IST" if value else ""


templates.env.filters["ist"] = ist
templates.env.filters["method_label"] = method_label
templates.env.globals["METHODS"] = METHODS
templates.env.globals["DESK_METHODS"] = DESK_METHODS
SCOPE_COOKIE = "rp"  # the property chosen in the sidebar switcher


def current_property(request: Request, auth: Auth) -> str:
    """The property picked in the sidebar ('' = all), only if this user may see it."""
    chosen = request.cookies.get(SCOPE_COOKIE, "")
    return chosen if chosen and auth.can_see(chosen) else ""


def page(request: Request, name: str, auth: Auth | None, **ctx):
    ctx.setdefault("toast", request.query_params.get("toast", ""))
    if auth:
        ctx.setdefault("side_props", auth.properties(get_repo(), active_only=True))
        ctx.setdefault("cur_prop", current_property(request, auth))
    return templates.TemplateResponse(request, name, {"auth": auth, "user": auth.user if auth else None,
                                                      "csrf": auth.csrf if auth else "", **ctx})
