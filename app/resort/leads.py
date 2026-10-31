"""Leads: what each WhatsApp guest asked for, saved while they chat, even if they never book.

One row per guest phone, always the latest enquiry: place, dates, persons, rooms, room type, the city they come
from, and how far they got (asked | summary | held). Kept in its own small table (created on first use) so the
core schema in db.py stays untouched. The dashboard's Guests page and its CSV export read it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.resort.db import ResortRepo

COLS = ("property_id", "check_in", "check_out", "adults", "children", "rooms", "room_type", "from_city", "stage",
        "booking_id")


def _ensure(repo: ResortRepo) -> None:
    repo._run("CREATE TABLE IF NOT EXISTS leads (phone TEXT PRIMARY KEY, property_id TEXT NOT NULL DEFAULT '', "
              "check_in TEXT, check_out TEXT, adults INTEGER NOT NULL DEFAULT 0, children INTEGER NOT NULL DEFAULT 0, "
              "rooms INTEGER NOT NULL DEFAULT 0, room_type TEXT NOT NULL DEFAULT '', from_city TEXT NOT NULL DEFAULT '', "
              "stage TEXT NOT NULL DEFAULT 'asked', booking_id TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)")


def get_lead(repo: ResortRepo, phone: str) -> dict[str, Any] | None:
    _ensure(repo)
    row = repo._one("SELECT * FROM leads WHERE phone=?", (phone,))
    return dict(row) if row else None


def save_lead(repo: ResortRepo, phone: str, now: datetime, **fields: Any) -> None:
    """Create or update the guest's lead. Fields left out (or None) keep their old value."""
    _ensure(repo)
    data: dict[str, Any] = {c: (get_lead(repo, phone) or {}).get(c) for c in COLS}
    data.update({k: v for k, v in fields.items() if k in COLS and v is not None})
    values = [data[c] if data[c] is not None else ("" if c not in ("check_in", "check_out") else None) for c in COLS]
    values = [0 if c in ("adults", "children", "rooms") and v in ("", None) else v for c, v in zip(COLS, values)]
    repo._run(f"INSERT OR REPLACE INTO leads(phone, {', '.join(COLS)}, updated_at) VALUES(?, {', '.join('?' * len(COLS))}, ?)",
              (phone, *values, now.astimezone(timezone.utc).isoformat()))


def list_leads(repo: ResortRepo) -> dict[str, dict[str, Any]]:
    """phone -> lead."""
    _ensure(repo)
    return {r["phone"]: dict(r) for r in repo._all("SELECT * FROM leads")}
