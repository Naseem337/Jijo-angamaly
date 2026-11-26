"""ONE open hold per guest (owner, 2026-11-26).

A guest's open hold is their newest unpaid booking together with the other parts of the same stay (a stay over two room
types is one group: same group_id and payment link). Every other unpaid booking of that guest is CANCELLED with
'[replaced by <id>]': its payment link is closed and its rooms are free again. Paid bookings are never touched.

Enforced after every new booking (tools._make_booking), before every guest turn (gate.handle) and by the sweeper (every
15 s, all guests), so holds left over from older code are cleaned too.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.phone import mask_phone

log = logging.getLogger(__name__)

REPLACED_OLD = "[replaced by a newer booking]"  # the old code's mark (it set those 'expired')
CLEANUP = "[duplicate hold closed]"


def _tags(b: Any) -> set[str]:
    """What ties the rows of one stay together: its group id and its payment link."""
    return {t for t in (f"g:{b.group_id}" if b.group_id else "", f"l:{b.payment_link_id}" if b.payment_link_id else "")
            if t} or {f"id:{b.id}"}


def keep_newest(bot: Any, phone: str, keep: set[str] | None = None, *, mark: str = "") -> list[str]:
    """Cancel every unpaid booking of this guest except `keep` (default: the newest hold and its group). Also turns the
    old code's 'expired, replaced by a newer booking' rows into cancelled ones. Returns the ids cancelled."""
    repo = bot.repo
    rows = repo.list_bookings(phone=phone, limit=200)
    holds = [b for b in rows if b.status == "hold"]
    if keep is None:
        if not holds:
            keep = set()
        else:
            newest = max(holds, key=lambda b: b.created_at)
            keep = {newest.id}
    # a mixed stay is ONE hold: every row sharing a group id or a payment link with a kept row is kept with it
    grown = True
    while grown:
        tags = {t for b in rows if b.id in keep for t in _tags(b)}
        more = {b.id for b in holds if b.id not in keep and _tags(b) & tags}
        grown, keep = bool(more), keep | more
    kept = [b for b in rows if b.id in keep]
    kept_links = {b.payment_link_id for b in kept if b.payment_link_id}
    new_id = next((b.id for b in kept if b.group_id in ("", b.id)), kept[0].id if kept else "")
    done: list[str] = []
    for b in rows:
        if b.id in keep:
            continue
        stale = b.status == "expired" and REPLACED_OLD in (b.notes or "")
        if b.status != "hold" and not stale:
            continue  # paid, checked in / out, cancelled, a plain expired hold: never touched
        if b.status == "hold" and b.payment_link_id and b.payment_link_id not in kept_links:
            try:
                bot.payments.cancel_link(b.payment_link_id)
            except Exception as exc:  # a link that cannot be closed must never stop the cleanup
                log.warning("Could not close the link of %s: %s", b.id, exc)
        note = (b.notes or "").replace(REPLACED_OLD, "").strip()
        if new_id and f"[replaced by {new_id}]" not in note:
            note = f"{note} [replaced by {new_id}]".strip()
        if mark and mark not in note:
            note = f"{note} {mark}".strip()
        b.status, b.notes = "cancelled", note
        repo.save_booking(b)
        done.append(b.id)
    if done:
        log.info("Holds: %d older unpaid booking(s) of %s cancelled: %s", len(done), mask_phone(phone), done)
    return done


def cleanup_all(bot: Any) -> dict[str, int]:
    """Every guest with more than one open hold: all but the newest stay are cancelled. {masked phone: count}."""
    phones = {b.guest_phone for b in bot.repo.list_bookings(status="hold", limit=5000)}
    out = {}
    for phone in phones:
        if n := len(keep_newest(bot, phone, mark=CLEANUP)):
            out[mask_phone(phone)] = n
    return out
