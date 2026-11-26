"""The guest's unfinished enquiry across days (owner, 2026-11-27).

The Enquiry (place, dates, adults, rooms, room picks, name / mobile of an earlier link) is kept for KEEP_DAYS, also over
the 12-hour new-conversation boundary. It is cleared only by 'restart' / 'reset all' (gate.py), a confirmed booking
(update_enquiry starts fresh after a paid one) or KEEP_DAYS without any message. Pure code on timestamps.

When the guest comes back in a NEW conversation, the old room check no longer counts: `checked` is dropped and
`saved_on` tells the AI to confirm the saved details first, then update_enquiry checks the rooms again NOW. Saved dates
already in the past are dropped (`dates_passed`). The name and mobile of an expired payment link for the same stay are
kept, so a free re-check goes straight to a new summary and NEW link (never the old one).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.resort.agent import conversation
from app.resort.agent.state import Enquiry
from app.resort.chat import RESTART_MARK

KEEP_DAYS = 7


def _previous_activity(bot: Any, phone: str) -> datetime | None:
    """When the guest or we last wrote BEFORE the message being answered now."""
    rows = [m for m in bot.repo.list_chat(phone, limit=5) if m.text != RESTART_MARK]
    if rows and rows[-1].direction == "in":
        rows = rows[:-1]
    return rows[-1].at if rows else None


def _mobile(notes: str) -> str:
    for part in (notes or "").split(";"):
        if part.strip().lower().startswith("mobile:"):
            return part.split(":", 1)[1].strip()
    return ""


def refresh(bot: Any, phone: str, conv: dict[str, Any]) -> Enquiry:
    """The saved enquiry for this turn: cleared after KEEP_DAYS quiet days, marked SAVED (old check dropped) in a new
    conversation. Returns it (saved when changed)."""
    enq = Enquiry.load(bot.repo, phone)
    if not enq.active or enq.paid:
        return enq
    now, today = bot.clock.now(), bot._today()
    last = _previous_activity(bot, phone)
    if last is not None and now - last > timedelta(days=KEEP_DAYS):
        fresh = Enquiry(language=enq.language)
        fresh.save(bot.repo, phone)
        return fresh
    try:
        started = datetime.fromisoformat(conv["started_at"])
        checked = datetime.fromisoformat(enq.checked_at) if enq.checked_at else None
    except (KeyError, TypeError, ValueError):
        return enq
    new_conv = checked is None or checked < started
    if not new_conv or bot._open_hold(phone):
        return enq  # the same conversation, or a live payment link for it: nothing to re-check yet
    changed = False
    if enq.checked is not None and not enq.saved_on:
        enq.saved_on = (checked or now).strftime("%d %b")
        enq.checked = None  # the old check never counts: update_enquiry checks the rooms again
        enq.bed_asked_for = enq.bed_answered_for = None
        changed = True
    if enq.check_in and enq.check_in < today:
        enq.check_in = enq.check_out = None
        enq.dates_passed, changed = True, True
    if not enq.contact and enq.place and enq.check_in:
        old = next(iter(bot.repo.list_bookings(phone=phone, limit=1)), None)
        if old is not None and old.status in ("expired", "cancelled") and old.property_id == enq.place \
                and old.check_in == enq.check_in and old.check_out == enq.check_out and old.guest_name:
            enq.contact = {"name": old.guest_name, "mobile": _mobile(old.notes) or "same", "old_ids": [old.id]}
            changed = True
    enq.booking_id, enq.link_open = None, False
    if changed:
        enq.save(bot.repo, phone)
    return enq
