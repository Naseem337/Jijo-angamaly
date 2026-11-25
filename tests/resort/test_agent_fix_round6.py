"""Eval fixes after the Haiku run (2026-11-25, 54/63): an acknowledgement never re-sends a recent step and never gets a
second AI ask; a room QUESTION shows the room, only a clear CHOICE switches it; the first discount answer; unrelated
questions. FAKE LLM only."""

from __future__ import annotations

from test_agent_fix_round5 import book_live
from test_agent_turn import ASK, STAY, go
from test_bot import GUEST, reply, tool, world  # noqa: F401  (world is a fixture)

from app.resort.agent.prompt import OWNER_RULES, SYSTEM
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import TOOL_SCHEMAS


def spec(name):
    return next(s for s in TOOL_SCHEMAS if s.name == name)


# --------------------------------------------------------------------------- 2. [ack] with a recent open step


def test_ok_right_after_the_name_ask_gets_sure_and_no_second_ai_ask(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    w.clock.advance(minutes=1)
    calls = len(w.llm.calls)
    res, _ = go(w, "ok", reply("[ack]"), live=True)
    assert res.sent == ["sure"] and w.wa.texts(GUEST)[-1] == "Sure."
    assert len(w.llm.calls) == calls + 1  # one AI call: [ack] is never asked again


def test_ok_right_after_the_payment_link_gets_sure_not_the_link_again(world):
    book_live(world)
    res, _ = go(world, "ok", reply("[ack]"), live=True)
    assert res.sent == ["sure"] and "pay" not in res.sent


def test_ok_twice_gets_sure_twice_never_a_second_ask(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    go(w, "ok", reply("[ack]"), live=True)
    calls = len(w.llm.calls)
    res, _ = go(w, "ok", reply("[ack]"), live=True)
    assert res.sent == ["sure"] and len(w.llm.calls) == calls + 1


def test_an_empty_reply_still_gets_the_second_ask(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    w.clock.advance(minutes=1)
    res, _ = go(w, "ok", reply(""), reply("Great, waiting for your details."), live=True)
    assert res.sent == ["ai"]


def test_ok_when_the_step_is_older_than_the_last_2_messages_gets_the_step(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    go(w, "is there a pool?", reply("Yes, there is a pool."), live=True)
    go(w, "and wifi?", reply("Yes, there is wifi."), live=True)
    res, _ = go(w, "ok", reply("[ack]"), live=True)
    assert res.sent == ["ask_contact"]


# --------------------------------------------------------------------------- 3. room question vs room choice


def test_the_prompt_and_tools_keep_a_room_question_apart_from_a_choice():
    assert "A QUESTION about another room ('do you have X also?', 'is there a cottage?', 'any deluxe?') → send_photos(kind=" \
           in SYSTEM
    assert "Only a clear CHOICE ('take X', 'change to X', 'X venam', 'book X'" in SYSTEM
    picks = spec("update_enquiry").parameters["properties"]["room_picks"]["description"]
    assert "ONLY a clear CHOICE ('take X', 'change to X', 'X venam', 'book X'" in picks
    assert "'do you have X also?'" in picks and "never room_picks" in picks
    photos = spec("send_photos").description
    assert "A QUESTION about another room ('do you have X also?'" in photos and "never switches their room" in photos
    assert "a room type or number" not in spec("update_enquiry").description


def test_a_room_question_answered_with_send_photos_never_switches_the_room(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    before = Enquiry.load(w.repo, GUEST).room_picks
    res, _ = go(w, "do you have a family suite also?", tool("send_photos", kind="room_type", room_type="Family Suite"),
                reply(""), live=True)
    assert [c["name"] for c in res.calls] == ["send_photos"]
    assert "room_picked" not in res.sent and Enquiry.load(w.repo, GUEST).room_picks == before


# --------------------------------------------------------------------------- 5. discount, first ask


def test_the_first_discount_answer_is_one_warm_line_with_a_real_good_point():
    assert ("the FIRST time the guest asks for a lower price, answer in one warm line with a real good point from FACTS "
            "that makes the price worth it (beach view, direct beach access, value), never an unrelated fact") \
        in OWNER_RULES


def test_the_first_discount_ask_gets_the_ais_line_and_no_handoff(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    line = "Every room faces the beach, so it's great value for the price."
    res, _ = go(w, "can you give some discount?", reply(line), live=True)
    assert res.sent[0] == "ai" and res.reply == line
    assert "handoff" not in res.sent and not any(k.startswith("staff:") for k in res.sent)


# --------------------------------------------------------------------------- 6. unrelated questions


def test_the_prompt_says_unrelated_questions_get_one_polite_line_only():
    assert ("A question unrelated to the resort or a stay (sports, news, general knowledge): one short polite line "
            "that you only\n  help with stays here (no resort or town name). No answer to the unrelated question.") in SYSTEM


def test_an_unrelated_question_gets_the_polite_line_and_no_handoff(world):
    line = "Sorry, I can only help with stays at our resort."
    res, _ = go(world, "who won the football world cup?", reply(line), live=True)
    assert res.sent == ["ai"] and res.reply == line
