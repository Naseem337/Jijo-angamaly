"""Rooms switched on after a big-group enquiry (owner, 2026-11-27).

When the owner switches a room type Active (or raises its room count) in the dashboard, every OPEN big-group enquiry of
that resort is re-checked live: not booked, check-in still ahead, the guest's last message within the 7-day memory.
A group that now fits:
  - guest's last message within 24 h (WhatsApp window) -> ONE message per enquiry (GUEST_TEXT) + the team is told
    (TEAM_SENT);
  - older -> no guest message; the team is asked to call (TEAM_CALL).
Never twice for the same enquiry (`proactive` in the guest's big-group record). The guest's "yes" goes to the AI, which
calls update_enquiry: the normal flow follows.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from app.core.phone import mask_phone

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
MEMORY = timedelta(days=7)
GUEST_TEXT = ("Good news! Rooms are now available for your {adults} people on {dates}. Shall I check and reserve them "
              "for you?")
TEAM_SENT = "✅ Guest {who} notified: rooms now available for {adults} people, {dates}."
TEAM_CALL = ("⚠️ Rooms now fit {adults} people for {who} ({dates}), but they last messaged over 24 h ago. Please call "
             "them: {phone}.")


def _dates(ci: date, co: date) -> str:
    return f"{ci.strftime('%d %b')} – {co.strftime('%d %b %Y')}"


def _last_in(bot: Any, phone: str) -> Any:
    return next((m.at for m in reversed(bot.repo.list_chat(phone, limit=60)) if m.direction == "in"), None)


def rooms_switched_on(bot: Any, property_id: str) -> dict[str, int]:
    """Re-check every open big-group enquiry of this resort; returns {'notified': n, 'team_call': n}."""
    from app.resort.agent.outbox import bot_outbox
    from app.resort.agent.tools import BIG_GROUP_KEY, AgentTools

    now, today = bot.clock.now(), bot._today()
    out = {"notified": 0, "team_call": 0}
    prop = bot.repo.get_property(property_id)
    if prop is None:
        return out
    for phone, state in bot.repo.list_states():
        rec = state.get(BIG_GROUP_KEY)
        if not isinstance(rec, dict) or rec.get("prop") != property_id or rec.get("booked") or rec.get("proactive"):
            continue
        try:
            ci, co, adults = date.fromisoformat(rec["ci"]), date.fromisoformat(rec["co"]), int(rec["adults"])
        except (KeyError, TypeError, ValueError):
            continue
        last = _last_in(bot, phone)
        if ci <= today or last is None or now - last > MEMORY:
            continue
        if any(b.status in ("paid", "checked_in", "checked_out") and b.check_in == ci
               for b in bot.repo.list_bookings(phone=phone, limit=20)):
            continue
        box = bot_outbox(bot, phone, dry_run=False)
        _, most = AgentTools(bot, phone, box)._free_capacity(prop, ci, co)  # live: active rooms right now
        if most < adults:
            continue
        guest = bot.repo.get_guest(phone)
        who = guest.name if guest and guest.name else mask_phone(phone)
        dates = _dates(ci, co)
        rec = {**rec, "proactive": now.isoformat()}
        if now - last <= WINDOW:
            box.text("group_fits_now", GUEST_TEXT.format(adults=adults, dates=dates))
            box.flush()
            rec["fits_told"] = True
            bot.alert_staff(TEAM_SENT.format(who=who, adults=adults, dates=dates), property_id)
            out["notified"] += 1
        else:
            bot.alert_staff(TEAM_CALL.format(who=who, adults=adults, dates=dates, phone=phone), property_id)
            out["team_call"] += 1
        state = bot.repo.get_state(phone)
        state[BIG_GROUP_KEY] = rec
        bot.repo.set_state(phone, state)
        log.info("Big group %s: rooms now fit %d adults (%s)", mask_phone(phone), adults,
                 "guest told" if rec.get("fits_told") else "team asked to call")
    return out
