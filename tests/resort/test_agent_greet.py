"""Live test 2026-11-25 (Haiku): 'hi' got 'Hi.' and 'hello' got 'Sure.'. The conversation boundary in code, the greet
tool, and 'Sure.' only for an acknowledgement the AI marked. FAKE LLM only."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from test_agent_fix_round5 import book_live
from test_agent_turn import ASK, STAY, go
from test_bot import GUEST, reply, tool, world  # noqa: F401  (world is a fixture)

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
    go(w, "is there a pool?", reply("Yes, there is a pool."), live=True)
    assert w.repo.get_state(GUEST)[conversation.STATE_KEY]["reason"] == "first"
    w.clock.advance(hours=13)
    go(w, "hi", tool("greet", language="en"), reply(""), live=True)
    conv = w.repo.get_state(GUEST)[conversation.STATE_KEY]
    assert conv["reason"] == "gap" and conv["started_at"] == w.clock.now().isoformat()


# --------------------------------------------------------------------------- 2. greet


def test_hi_in_a_new_conversation_gets_the_owners_greeting(world):
    res, _ = go(world, "hi", tool("greet", language="en"), reply(""), live=True)
    assert res.sent == ["greet"] and world.wa.texts(GUEST)[-1] == GREET


def test_hello_after_12_hours_is_a_new_conversation_again(world):
    w = world
    go(w, "is there a pool?", reply("Yes, there is a pool."), live=True)
    w.clock.advance(hours=12, minutes=5)
    res, _ = go(w, "hello", tool("greet", language="en"), reply(""), live=True)
    assert res.sent == ["greet"] and w.wa.texts(GUEST)[-1] == GREET


def test_hi_mid_conversation_with_nothing_open_is_a_short_hi(world):
    w = world
    go(w, "is there a pool?", reply("Yes, there is a pool."), live=True)
    res, _ = go(w, "hello", tool("greet", language="en"), reply(""), live=True)
    assert res.sent == ["hello"] and w.wa.texts(GUEST)[-1] == "Hi."


def test_hi_mid_conversation_with_an_open_enquiry_gives_the_stay_and_the_open_step(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)  # ends with the name and mobile ask
    go(w, "is there a pool?", reply("Yes, there is a pool."), live=True)
    res, _ = go(w, "hai", tool("greet", language="en"), reply(""), live=True)
    assert res.sent == ["hello_back", "ask_contact"]
    hello = w.wa.texts(GUEST)[-2]
    assert hello.startswith("Hi. Your enquiry: ") and "10 Nov to 12 Nov" in hello and "2 person(s)" in hello


def test_the_open_step_is_not_repeated_when_it_is_the_last_message(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)  # the name and mobile ask is the last message
    res, _ = go(w, "hi", tool("greet", language="en"), reply(""), live=True)
    assert res.sent == ["hello_back"]


def test_hi_with_an_open_link_gives_the_stay_and_the_link(world):
    w = world
    book_live(w)
    go(w, "is there wifi?", reply("Yes, there is wifi."), live=True)
    res, _ = go(w, "hi", tool("greet", language="en"), reply(""), live=True)
    assert res.sent == ["hello_back", "pay"]


def test_hi_after_a_confirmed_booking(world):
    w = world
    book_live(w)
    held = w.repo.list_bookings(phone=GUEST, status="hold", limit=1)[0]
    w.bot.mark_paid(booking_id=held.id, payment_id="pay_test")  # writes RESTART_MARK: a new conversation
    w.clock.advance(minutes=1)
    res, _ = go(w, "hi", tool("greet", language="en"), reply(""), live=True)
    assert res.sent == ["greet"]  # the first hello of the new conversation
    w.clock.advance(minutes=1)
    res, _ = go(w, "hello", tool("greet", language="en"), reply(""), live=True)
    assert res.sent == ["hello_booked"] and "is confirmed ✅" in w.wa.texts(GUEST)[-1]


def test_greet_runs_once_and_says_when_to_use_it():
    spec = next(s for s in TOOL_SCHEMAS if s.name == "greet")
    assert "'hi', 'hello', 'hai', 'vanakkam', 'namaste', 'good morning'" in spec.description
    assert spec.parameters["required"] == ["language"]


# --------------------------------------------------------------------------- 3. 'Sure.' only for an acknowledgement


def test_hello_the_ai_left_empty_never_gets_sure(world):
    w = world
    go(w, "is there a pool?", reply("Yes, there is a pool."), live=True)
    res, _ = go(w, "hello", reply(""), tool("greet", language="en"), reply(""), live=True)  # asked once more
    assert "sure" not in res.sent and res.sent == ["hello"]


def test_an_unmarked_empty_reply_is_never_sure_even_when_the_ai_fails_again(world):
    w = world
    go(w, "is there a pool?", reply("Yes, there is a pool."), live=True)
    res, _ = go(w, "what time is check in?", reply(""), live=True)  # the second ask has no scripted reply
    assert "sure" not in res.sent and res.sent == ["say_again_short"]


def test_ok_marked_as_an_acknowledgement_gets_sure(world):
    w = world
    go(w, "is there a pool?", reply("Yes, there is a pool."), live=True)
    res, _ = go(w, "ok thanks", reply("[ack]"), live=True)
    assert res.sent == ["sure"] and res.reply == ""


def test_the_marker_is_never_sent_to_the_guest(world):
    w = world
    go(w, "is there a pool?", reply("Yes, there is a pool."), live=True)
    go(w, "ok", reply("[ack]"), live=True)
    assert "[ack]" not in " ".join(w.wa.texts(GUEST))
    assert all("[ack]" not in m.text for m in w.repo.list_chat(GUEST, limit=50) if m.direction == "out")


def test_the_session_boundary_ignores_the_guests_words(world):
    """A greeting after a short pause is NOT a new conversation (only restart / a confirmed booking / 12 h are)."""
    w = world
    w.repo.add_chat(ChatMessage(GUEST, "out", "Yes, there is a pool.", w.clock.now() - timedelta(hours=3), "text"))
    res, _ = go(w, "Hi", tool("greet", language="en"), reply(""), live=True)
    assert res.sent == ["hello"]
