"""Step AI-3: the prompt and the agent loop, with a FAKE LLM (scripted replies and tool calls; no real API call anywhere)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from test_agent_photos import pw  # noqa: F401  (fixture: the test resort with a glimpse and photos)
from test_bot import GUEST, STAFF, reply, tool, world  # noqa: F401  (world is a fixture)

from app.providers.llm import LLMError, LLMResponse, ToolCall
from app.resort.agent import prompt as prompt_module
from app.resort.agent import turn as turn_module
from app.resort.agent.outbox import bot_outbox
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import TOOL_SCHEMAS
from app.resort.agent.turn import HISTORY, MAX_ROUNDS, history, run_turn, swipe_note
from app.resort.chat import RESTART_MARK
from app.resort.models import ChatMessage

STAY = {"place": "Alleppey", "check_in": "2026-11-10", "check_out": "2026-11-12", "adults": 2}
ASK = "Alleppey 10 nov to 12 nov 2 persons"


class Spy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            return "wamid.spy"
        return call


def go(w, text, *script, live=False, swipe="", save=True):
    """One guest turn against the scripted fake LLM. The guest's message is stored in the chat first, as the gate will."""
    if save:
        w.repo.add_chat(ChatMessage(GUEST, "in", text, w.clock.now(), "text"))
    w.llm.script.extend(script)
    box = bot_outbox(w.bot, GUEST, dry_run=not live)
    return run_turn(w.bot, w.llm, GUEST, text, box, swipe=swipe), box


def texts(box):
    return {i.key: i.text for i in box.sent}


# --------------------------------------------------------------------------- the loop


def test_a_tool_call_is_run_its_result_is_fed_back_and_the_final_text_is_the_answer(world):
    w = world
    res, box = go(w, ASK + ", is there a pool?", tool("update_enquiry", **STAY), reply("Yes, there is a pool."))
    assert res.outcome == "answered" and res.rounds == 1 and res.reply == "Yes, there is a pool."
    assert [c["name"] for c in res.calls] == ["update_enquiry"] and res.calls[0]["result"]["ok"]
    assert len(w.llm.calls) == 2
    second = w.llm.calls[1].messages
    assert [m.role for m in second[-2:]] == ["assistant", "tool"]  # the AI's call, then what the tool answered
    assert second[-2].tool_calls[0].name == "update_enquiry" and second[-1].tool_call_id == second[-2].tool_calls[0].id
    fed = json.loads(second[-1].content)
    assert set(fed) == {"ok", "facts", "sent", "next_step", "note"} and fed["ok"] is True
    assert fed["sent"] == ["rooms_ok", "rooms_plain", "room_label", "ask_contact"] and fed["next_step"] == "ask_contact"
    assert not second[-1].is_error


def test_a_failed_tool_result_is_fed_back_as_an_error(world):
    w = world
    res, _ = go(w, "price?", tool("get_price"), reply("Which dates, sir?"))
    assert w.llm.calls[1].messages[-1].is_error and json.loads(w.llm.calls[1].messages[-1].content)["ok"] is False


def test_several_tool_calls_in_one_reply_all_run_in_order(world):
    w = world
    two = LLMResponse("", [ToolCall("a", "update_enquiry", STAY), ToolCall("b", "get_price", {})], "tool_calls")
    res, box = go(w, ASK + " and the price", two, reply("There you go."))
    assert [c["name"] for c in res.calls] == ["update_enquiry", "get_price"] and res.rounds == 1
    tool_msgs = [m for m in w.llm.calls[1].messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["a", "b"]


def test_at_most_4_rounds_of_tool_calls_run(world):
    w = world
    script = [tool("get_price", call_id=f"c{i}") for i in range(MAX_ROUNDS + 1)]  # the AI never stops asking
    res, box = go(w, "price?", *script)
    assert MAX_ROUNDS == 4 and res.rounds == 4 and res.hit_limit
    assert len(res.calls) == 4 and w.llm.script == []  # the 5th reply's call is not run
    assert len(w.llm.calls) == 6  # 5 + the never-silent second ask (live test 2026-11-25)
    assert len([m for m in w.llm.calls[4].messages if m.role == "tool"]) == 4
    assert res.outcome == "never_silent"  # nothing was said: the open step goes


def test_a_bad_tool_call_from_the_ai_is_reported_back_and_never_crashes(world):
    w = world
    broken = LLMResponse("", [ToolCall("x", "update_enquiry", {}, arguments_error="arguments must be a JSON object")],
                         "tool_calls")
    res, box = go(w, "hello", broken, tool("no_such_tool"), reply("Sorry, could you say it again?"))
    assert res.outcome == "answered" and res.rounds == 2 and box.keys == ["ai"]
    first, second = (json.loads(c["result"] and json.dumps(c["result"])) for c in res.calls)
    assert not first["ok"] and "JSON object" in first["facts"]["problem"] and not second["ok"]


# --------------------------------------------------------------------------- the output order (design section 4)


def test_the_ais_own_line_goes_first_then_the_tool_messages_in_order(world):
    w = world
    res, box = go(w, ASK + ", is there a pool?", tool("update_enquiry", **STAY), reply("Yes, there is a pool."))
    assert res.sent == ["ai", "rooms_ok", "rooms_plain", "room_label", "ask_contact"]
    assert [i.text for i in box.sent][0] == "Yes, there is a pool." and box.sent[1].text == "Yes, rooms are available for your dates."


def test_the_order_holds_when_it_goes_out_live(world):
    w = world
    go(w, ASK + ", is there a pool?", tool("update_enquiry", **STAY), reply("Yes, there is a pool."), live=True)
    assert w.wa.texts(GUEST) == ["Yes, there is a pool.", "Yes, rooms are available for your dates.",
                                 "For 2 persons: 1 room(s).", "*Deluxe*", "Please share your name and mobile number to proceed."]


def test_the_ais_line_comes_before_photos_too(pw):
    w = pw
    res, box = go(w, ASK + ", is there a pool?", tool("update_enquiry", **STAY), reply("Yes, there is a pool."))
    assert res.sent[0] == "ai" and res.sent[1:4] == ["rooms_ok", "rooms_plain", "glimpse"] and "photos:3" in res.sent


def test_no_reply_marker_from_the_ai_is_not_sent(world):
    w = world
    res, box = go(w, ASK, tool("update_enquiry", **STAY), reply("[no reply]"))
    assert res.reply == "" and res.sent == ["rooms_ok", "rooms_plain", "room_label", "ask_contact"]


# --------------------------------------------------------------------------- never silent


def test_nothing_sent_means_the_open_step_goes(world):
    w = world
    res, box = go(w, "hmm", reply("[no reply]"))  # nothing is open (live test 2026-11-25): never silent -> say again
    assert res.outcome == "never_silent" and res.sent == ["say_again_short"]
    Enquiry(place=w.p1.id).save(w.repo, GUEST)
    again, box = go(w, "hmm", reply(""))
    assert again.outcome == "never_silent" and again.sent == ["form"]


def test_nothing_sent_and_nothing_open_asks_to_say_it_again(world):
    w = world
    from datetime import date
    Enquiry(place=w.p1.id, check_in=date(2026, 11, 10), check_out=date(2026, 11, 12), adults=2, booking_id="x",
            paid=True, checked={"available": True}).save(w.repo, GUEST)
    res, box = go(w, "hmm", reply(""))
    assert res.outcome == "never_silent" and res.sent == ["say_again_short"]
    assert texts(box)["say_again_short"] == "Sorry, could you say that again?"


def test_an_answer_alone_is_enough_no_extra_step(world):
    res, box = go(world, "is there a pool?", reply("Yes, there is a pool."))
    assert res.outcome == "answered" and res.sent == ["ai"]


# --------------------------------------------------------------------------- the LLM fails


def test_an_llm_error_gets_ai_down_and_one_staff_alert_per_30_minutes(world):
    w = world
    first, _ = go(w, "hello?", LLMError("mock", "down"), live=True)
    assert first.outcome == "ai_down" and first.sent == ["ai_down", "staff:ai_down"] and "down" in first.error
    down = w.wa.texts(GUEST)[-1]
    assert down.startswith("Sorry") and "contact the team directly" in down and "9497861924" in down
    alerts = lambda: [m for m in w.wa.texts(STAFF) if m.startswith("Bot could not answer a message from")]  # noqa: E731
    assert len(alerts()) == 1 and "hello?" in alerts()[0]
    second, _ = go(w, "anyone there?", LLMError("mock", "down"), live=True)  # still down, 1 minute later
    assert second.sent == ["ai_down"] and len(alerts()) == 1 and w.wa.texts(GUEST)[-1] == down  # told, not alerted again
    w.clock.advance(minutes=31)
    third, _ = go(w, "hello??", LLMError("mock", "down"), live=True)
    assert third.sent == ["ai_down", "staff:ai_down"] and len(alerts()) == 2


def test_a_dry_run_ai_down_only_records_and_never_uses_up_the_alert_slot(world):
    w = world
    spy = Spy()
    w.bot.wa = spy
    a, box = go(w, "hello?", LLMError("mock", "down"))
    b, box2 = go(w, "hello?", LLMError("mock", "down"))
    assert a.sent == ["ai_down", "staff:ai_down"] and b.sent == ["ai_down", "staff:ai_down"] and spy.calls == []
    assert getattr(w.bot, "_down_alerted", None) is None


def test_no_llm_at_all_is_ai_down(world):
    w = world
    box = bot_outbox(w.bot, GUEST, dry_run=True)
    res = run_turn(w.bot, None, GUEST, "hi there", box)
    assert res.outcome == "ai_down" and res.sent == ["ai_down", "staff:ai_down"]


def test_the_llm_failing_after_a_tool_already_spoke_does_not_add_ai_down_text(world):
    w = world
    res, box = go(w, ASK, tool("update_enquiry", **STAY), LLMError("mock", "down"))
    assert res.outcome == "ai_down" and res.sent == ["rooms_ok", "rooms_plain", "room_label", "staff:ai_down", "ask_contact"]


# --------------------------------------------------------------------------- what the AI sees


def test_the_swipe_note_is_a_line_just_before_the_guest_message(world):
    w = world
    note = "[Guest swiped-replied to the photo of Room 108 — Beach View Standard]"
    go(w, "this one venam", reply("Sure."), swipe=note)
    last = w.llm.calls[0].messages[-1]
    assert last.role == "user" and last.content.splitlines()[-2:] == [note, "this one venam"]


def test_the_swipe_note_goes_only_on_the_latest_of_several_messages(world):
    w = world
    w.repo.add_chat(ChatMessage(GUEST, "in", "rooms for 4", w.clock.now(), "text"))
    go(w, "this one venam", reply("Sure."), swipe="[NOTE]")
    assert w.llm.calls[0].messages[-1].content == "rooms for 4\n[NOTE]\nthis one venam"


def test_swipe_note_finds_the_room_of_a_photo_a_label_and_other_messages(pw):
    w = pw
    go(w, ASK, tool("update_enquiry", **STAY), reply("[no reply]"), live=True)
    rows = w.repo.list_chat(GUEST, limit=300)
    photo = next(m for m in rows if m.text == f"[photo] {w.refs['deluxe'][0]}")
    assert swipe_note(w.bot, GUEST, photo.wamid) == "[Guest swiped-replied to the photo of Deluxe]"
    ask = next(m for m in rows if m.text.startswith("Please share your name"))
    assert swipe_note(w.bot, GUEST, ask.wamid) == f'[Guest swiped-replied to your message: "{ask.text}"]'
    assert swipe_note(w.bot, GUEST, "wamid.unknown") == "" and swipe_note(w.bot, GUEST, None) == ""


def test_the_prompt_has_no_rupee_amount_and_all_its_blocks(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply("[no reply]"))
    go(w, "ok", reply("Fine."))  # a second turn: the checked stay is now in CURRENT ENQUIRY
    for call in w.llm.calls:
        system = call.system
        assert "₹" not in system and "Rs " not in system and "rupee" not in system.lower()
        for amount in ("4000", "4,000", "8000", "8,000", "9500"):
            assert amount not in system
        assert "Resort Booking Assistant" in system and "{brand}" not in system
        for block in ("TRUTH", "BOOKING", "HOW YOU TALK", "OWNER RULES", "TODAY\nToday is Mon 2 Nov 2026 (2026-11-02)", "FACTS\n",
                      "GUEST RECORDS", "CURRENT ENQUIRY"):
            assert block in system, block
    last = w.llm.calls[-1].system
    assert "checked: available — 1 Deluxe" in last and "PLACE 'Pozhiyoram, Alleppey'" in last
    assert "Extra bed: available (its cost comes only from the tools)" in last  # the facts carry no amounts


def test_the_owner_rules_are_in_the_prompt(world):
    w = world
    go(w, "hi", reply("Hi, How may I assist you?"))
    system = w.llm.calls[0].system
    for line in ("FIRST time the guest asks for a lower price", 'SECOND time, talk_to_owner(reason="other")', "3 or more separate questions",
                 "state only the charge in if_cancelled_today", "wants a NEW booking", "Please bring your ID proof while coming."):
        assert line in system, line


def test_every_call_offers_the_12_tools_at_low_temperature(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply("Done."))
    assert len(w.llm.calls) == 2
    for call in w.llm.calls:
        assert call.tools == TOOL_SCHEMAS and len(call.tools) == 12
        assert call.temperature == 0.2 and call.max_tokens == 300


def test_no_model_name_is_hard_coded_in_the_agent(world):
    for module in (turn_module, prompt_module):
        source = Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "gpt", "gemini", "sarvam", "llama", "deepseek", "qwen", "mistral", "opus", "sonnet"):
            assert name not in source, (module.__name__, name)


def test_temperature_reaches_the_provider_only_when_asked():
    from app.providers.llm import FallbackLLM, MockLLM
    inner = MockLLM([MockLLM.text("a"), MockLLM.text("b")])
    chain = FallbackLLM([inner])
    chain.complete("s", [], None, 100)
    chain.complete("s", [], None, 100, temperature=0.3)
    assert [c.temperature for c in inner.calls] == [None, 0.3]


# --------------------------------------------------------------------------- the chat history


def say(w, direction, text, minutes_ago):
    w.repo.add_chat(ChatMessage(GUEST, direction, text, w.clock.now() - timedelta(minutes=minutes_ago), "text"))


def test_the_ai_sees_the_last_12_real_messages_ending_with_the_new_one(world):
    w = world
    for i in range(30):
        say(w, "in" if i % 2 == 0 else "out", f"message {i}", 100 - i)
    go(w, "the new one", reply("ok"))
    messages = w.llm.calls[0].messages
    assert messages[0].role == "user" and messages[-1].content.endswith("the new one")
    lines = [ln for m in messages for ln in m.content.splitlines()]
    assert len(lines) <= HISTORY + 1 and "message 0" not in lines and "message 29" in lines
    assert HISTORY == 12 and "message 17" not in lines


def test_the_new_message_is_added_once_whether_or_not_it_was_stored(world):
    w = world
    go(w, "stored one", reply("ok"))
    go(w, "not stored", reply("ok"), save=False)
    first, second = w.llm.calls
    assert first.messages[-1].content.count("stored one") == 1
    assert second.messages[-1].content.endswith("not stored") and second.messages[-1].content.count("not stored") == 1


def test_photos_are_one_note_and_links_and_log_lines_never_reach_the_ai(world):
    w = world
    say(w, "in", "a question before the restart", 40)
    say(w, "out", RESTART_MARK, 39)  # a new conversation starts here: what came before is not shown
    say(w, "in", "photos please", 30)
    for i in range(3):
        say(w, "out", f"[photo] /resort/media/{i}.jpg", 29)
    say(w, "out", "Pay here:\nhttps://rzp.io/abc and also http://x.y", 28)
    say(w, "out", "[invoice] Invoice-1.pdf", 27)
    say(w, "in", "[image]", 26)
    say(w, "out", "[template pending] hello", 25)
    say(w, "in", "hi", 23)
    go(w, "price?", reply("ok"))
    blob = "\n".join(m.content for m in w.llm.calls[0].messages)
    assert "[photos sent: 3]" in blob and "/resort/media/" not in blob
    assert "rzp.io" not in blob and "http" not in blob and "[link]" in blob
    assert "[invoice]" not in blob and "[image]" not in blob and "[template" not in blob and RESTART_MARK not in blob
    assert "before the restart" not in blob and "photos please" in blob


def test_a_24_hour_gap_starts_a_new_conversation(world):
    w = world
    say(w, "in", "old question about cottages", 60 * 24 * 3)
    say(w, "out", "old answer", 60 * 24 * 3 - 1)
    say(w, "in", "recent hello", 5)
    go(w, "price?", reply("ok"))
    blob = "\n".join(m.content for m in w.llm.calls[0].messages)
    assert "recent hello" in blob and "old question" not in blob and "old answer" not in blob


# --------------------------------------------------------------------------- a dry run never touches WhatsApp


def test_a_full_dry_run_turn_through_the_loop_makes_zero_whatsapp_calls(pw):
    w = pw
    spy = Spy()
    w.bot.wa = spy
    res, box = go(w, ASK + ", photos?", tool("update_enquiry", **STAY), tool("send_photos", kind="all_rooms"),
                  reply("Here you are."))
    assert res.sent[0] == "ai" and any(k.startswith("photos:") for k in res.sent) and spy.calls == []
