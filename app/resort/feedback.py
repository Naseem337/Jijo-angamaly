"""Guest feedback after checkout: a 1-5 rating and an optional comment, one row per booking.

Kept in its own small table (created on first use) so the core schema in db.py stays untouched.
The dashboard can show scores with list_feedback() and average_rating().
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.resort.db import ResortRepo


def _ensure(repo: ResortRepo) -> None:
    repo._run("CREATE TABLE IF NOT EXISTS feedback (booking_id TEXT PRIMARY KEY, property_id TEXT NOT NULL, "
              "rating INTEGER NOT NULL, comment TEXT NOT NULL DEFAULT '', at TEXT NOT NULL)")


def save_feedback(repo: ResortRepo, booking_id: str, rating: int, comment: str, at: datetime) -> None:
    """Create or update the feedback of a booking (the comment arrives after the rating)."""
    booking = repo.get_booking(booking_id)
    if booking is None:
        raise ValueError(f"unknown booking {booking_id}")
    _ensure(repo)
    repo._run("INSERT INTO feedback(booking_id, property_id, rating, comment, at) VALUES(?,?,?,?,?) "
              "ON CONFLICT(booking_id) DO UPDATE SET rating=excluded.rating, comment=excluded.comment, at=excluded.at",
              (booking_id, booking.property_id, max(1, min(5, int(rating))), comment.strip()[:1000],
               at.astimezone(timezone.utc).isoformat()))


def list_feedback(repo: ResortRepo, property_id: str | None = None) -> list[dict[str, Any]]:
    """Newest first: booking_id, property_id, rating, comment, at (ISO UTC)."""
    _ensure(repo)
    if property_id:
        rows = repo._all("SELECT * FROM feedback WHERE property_id=? ORDER BY at DESC", (property_id,))
    else:
        rows = repo._all("SELECT * FROM feedback ORDER BY at DESC")
    return [dict(r) for r in rows]


def average_rating(repo: ResortRepo, property_id: str | None = None) -> float | None:
    rows = list_feedback(repo, property_id)
    return round(sum(r["rating"] for r in rows) / len(rows), 2) if rows else None
