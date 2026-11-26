"""Live test 2026-11-25 (Haiku): 'hi' got 'Hi.' and 'hello' got 'Sure.'. The conversation boundary in code, the greet
tool, and 'Sure.' only for an acknowledgement the AI marked. FAKE LLM only."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from test_agent_fix_round5 import book_live
from test_agent_turn import ASK, STAY, go
from test_bot import GUEST, answer, reply, tool, world  # noqa: F401  (world is a fixture)

from app.resort.agent import conversation
from app.resort.agent.tools import TOOL_SCHEMAS
from app.resort.chat import RESTART_MARK
from app.resort.models import ChatMessage

GREET = "Hi, How may I assist you?"


def row(text, at, direction="in"):
    return SimpleNamespace(text=text, at=at, direction=direction)


# --------------------------------------------------------------------------- 1. the boundary


def test_a_new_conversation_starts_after_restart_a_confirmed_booking_or_12_hours(world):
    now = world.clock.now()
    rows = [row("a", now - timedelta(hours=30)), row("b", now - timedelta(hours=20)),
            row(RESTART_MARK, now - timedelta(hours=19), "out"), row("c", now - timedelta(hours=18))]
    assert conversation.start_index(rows) == 3  # after the marker
    rows += [row("d", now - timedelta(hours=6, minutes=1)), row("e", now)]  # an 11 h 59 m gap: same conversation
    assert conversation.start_index(rows) == 3
    rows.append(row("f", now + timedelta(hours=12)))  # 12 h: a new one
    assert conversation.start_index(rows) == 6


def test_the_boundary_is_stored_per_guest(world):
    w = world
    go(w, "is there a pool?", answer("Yes, there is a pool."), live=True)
    assert w.repo.get_state(GUEST)[conversation.STATE_KEY]["reason"] == "first"
    w.clock.advance(hours=13)
    go(w, "hi", tool("greet", language="en"), reply(""), live=True)
    conv = w.repo.get_state(GUEST)[conversation.STATE_KEY]
    assert conv["reason"] == "gap" and conv["started_at"] == w.clock.now().isoformat()


# --------------------------------------------------------------------------- 2. greet


# --------------------------------------------------------------------------- 3. 'Sure.' only for an acknowledgement


