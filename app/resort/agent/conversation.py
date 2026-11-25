"""Where the guest's current conversation begins (live test 2026-11-25: 'hi' got 'Hi.' and 'hello' got 'Sure.').

A NEW conversation starts:
  - after 'restart' or a confirmed booking (both store RESTART_MARK in the chat log), or
  - after GAP (12 hours) with no message either way.
Pure code on the chat log's timestamps and the exact marker row: guest words never decide it.

The boundary is also stored per guest in the chat state (key `agent_conv`: when the conversation started and why), so it
can be read without the chat log (dashboard, logs).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.resort.chat import RESTART_MARK

GAP = timedelta(hours=12)
STATE_KEY = "agent_conv"


def start_index(rows: list[Any]) -> int:
    """Index of the first row of the current conversation in `rows` (oldest first)."""
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].text == RESTART_MARK:
            return i + 1
        if i and rows[i].at - rows[i - 1].at >= GAP:
            return i
    return 0


def _reason(rows: list[Any], i: int) -> str:
    if i == 0:
        return "first"
    if rows[i - 1].text == RESTART_MARK:
        return "restart"  # 'restart' or a confirmed booking
    return "gap"


def current(bot: Any, phone: str, limit: int = 200) -> list[Any]:
    """The rows of the current conversation (the marker itself excluded)."""
    rows = bot.repo.list_chat(phone, limit=limit)
    return [m for m in rows[start_index(rows):] if m.text != RESTART_MARK]


def we_spoke(bot: Any, phone: str) -> bool:
    """We already wrote to the guest in this conversation: a 'hi' now is a mid-conversation hello."""
    return any(m.direction == "out" for m in current(bot, phone))


def remember(bot: Any, phone: str) -> dict[str, Any]:
    """Store where the current conversation began ({started_at, reason}) in the guest's state; returns it."""
    rows = bot.repo.list_chat(phone, limit=200)
    i = start_index(rows)
    first = next((m for m in rows[i:] if m.text != RESTART_MARK), None)
    conv = {"started_at": first.at.isoformat() if first is not None else bot.clock.now().isoformat(),
            "reason": _reason(rows, i)}
    state = bot.repo.get_state(phone)
    if state.get(STATE_KEY) != conv:
        state[STATE_KEY] = conv
        bot.repo.set_state(phone, state)
    return conv
