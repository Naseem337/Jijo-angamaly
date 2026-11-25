"""Agent fix round 2 (eval failures of round 1, no repeats, team wording, policies), with the FAKE LLM."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml
from test_agent_fix_round1 import late_month, run
from test_agent_tools_2c import book
from test_agent_turn import ASK, STAY, go, texts
from test_bot import GUEST, reply, tool, world  # noqa: F401  (world is a fixture)

from app.providers.llm import MockLLM
from app.resort.agent import guard
from app.resort.agent.outbox import Outbox, near_same
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import LANGUAGE_FIELD, ONCE_NOTE, TOOL_SCHEMAS
from app.resort.i18n import t

ROOT = Path(__file__).resolve().parents[2]
GUEST_CALLED = "Thank you sir. Please let me know if you need any help with your booking."


def spec(name):
    return next(s for s in TOOL_SCHEMAS if s.name == name)


def system_prompt(w):
    go(w, "hi", reply("Hi, How may I assist you?"))
    return w.llm.calls[-1].system


def live_link_world(w):
    book(w, live=True)  # update_enquiry STAY + create_booking: the payment link is open
    w.clock.advance(minutes=1)


# --------------------------------------------------------------------------- a) month_given


def test_month_given_is_gone_dates_are_objects():  # fix round 3: {day, month, year} replaced month_given
    props = spec("update_enquiry").parameters["properties"]
    assert "month_given" not in props and props["check_in"]["properties"].keys() == {"day", "month", "year"}


def test_a_day_alone_that_passed_this_month_is_next_month(world):
    late_month(world)  # today 2026-11-25
    result, sent, _ = run(world, "update_enquiry", {"place": "Alleppey", "check_in": {"day": 20},
                                                    "check_out": {"day": 22}, "adults": 2})
    assert result["ok"] and result["facts"]["check_in"] == "2026-12-20" and "past_date" not in sent


def test_a_future_check_in_needs_no_month_given_to_work(world):
    result, _, _ = run(world, "update_enquiry", STAY)
    assert result["ok"] and result["facts"]["check_in"] == STAY["check_in"]


# --------------------------------------------------------------------------- b) c) g) descriptions and prompt


def test_a_place_on_its_own_is_booking_information(world):
    line = "A place or resort name on its own ('Alleppey', 'kanthaloor', even misspelt) IS booking information"
    assert line in spec("update_enquiry").description
    assert line in system_prompt(world)


def test_the_language_field_explains_each_style_with_example_words():
    desc = LANGUAGE_FIELD["description"]
    assert "manglish = Malayalam words in English letters (venam, undo, aano, alle)" in desc
    assert "tanglish = Tamil words in English letters (venum, irukka, irukkom, vanakkam, la)" in desc
    assert "hinglish = Hindi words in English letters (chahiye, hai, kya)" in desc
    assert spec("update_enquiry").parameters["properties"]["language"] is LANGUAGE_FIELD


def test_a_wanted_room_type_goes_in_room_picks_and_exclude_only_for_a_refused_type():
    props = spec("update_enquiry").parameters["properties"]
    assert "Guest wants a room type → room_picks=[that type]" in props["room_picks"]["description"]
    assert props["exclude_room_types"]["description"].startswith("use ONLY when the guest says they don't want a type")


# --------------------------------------------------------------------------- d) guest_called: the exact text


def test_guest_called_goes_word_for_word_with_sir_and_the_ais_line_is_dropped(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply("[no reply]"), live=True)
    w.clock.advance(minutes=1)
    go(w, "is there parking?", reply("Yes, there is parking."), live=True)  # past the start: 'sir' is eased elsewhere
    w.clock.advance(minutes=1)
    res, _ = go(w, "I already called the owner", tool("talk_to_owner", reason="guest_called"), reply("Got it."),
                live=True)
    assert res.sent == ["guest_called", "ask_contact"] and res.reply == ""
    assert GUEST_CALLED in w.wa.texts(GUEST)
    assert not [m for m in w.wa.sent if m[0] != GUEST]  # no staff alert


# --------------------------------------------------------------------------- e) mid-chat hi


def test_how_may_i_assist_you_only_as_the_first_greeting(world):
    w = world
    res, box = go(w, "hi", reply("Hi, How may I assist you?"))
    assert texts(box)["ai"] == "Hi, How may I assist you?"
    go(w, ASK, tool("update_enquiry", **STAY), reply("[no reply]"), live=True)
    w.clock.advance(minutes=1)
    res, box = go(w, "hi", reply("Hi, How may I assist you?"))
    assert res.sent == ["ai"] and texts(box)["ai"] == "Hi."  # fix round 5: the step was among the last 2 messages and only the AI spoke: not re-sent


@pytest.mark.parametrize("line,kept", [("Hi sir! How can I help you?", "Hi sir!"), ("How may I assist you?", "Hi."),
                                       ("Yes, there is a pool.", "Yes, there is a pool.")])
def test_the_greeting_question_is_dropped_mid_chat(line, kept):
    assert guard.mid_chat_greeting(line) == kept


# --------------------------------------------------------------------------- f) a change after the link


def test_new_dates_after_the_link_send_the_new_summary_and_link(world):
    w = world
    live_link_world(w)
    old = w.wa.texts(GUEST)[-1]
    room = w.repo.list_room_types(w.p1.id)[0].name
    result, sent, _ = run(w, "update_enquiry", {**STAY, "place": "Pozhiyoram, Alleppey", "check_in": "2026-11-15",
                                                "check_out": "2026-11-17", "rooms": 1, "month_given": True,
                                                "room_picks": [{"room_type": room, "count": 1}]}, live=True)
    assert result["sent"][-2:] == ["summary", "pay"] and "room_picked" not in result["sent"]
    assert "15th Nov" in sent["summary"] and sent["pay"] != old and result["next_step"] == "pay"


def test_the_same_stay_as_the_link_still_sends_nothing(world):
    w = world
    live_link_world(w)
    result, sent, _ = run(w, "update_enquiry", STAY, live=True)
    assert result["sent"] == [] and "unchanged" in result["note"]


# --------------------------------------------------------------------------- h) the link again


def test_link_lost_while_the_hold_is_live_sends_the_same_link(world):
    w = world
    live_link_world(w)
    link = w.wa.texts(GUEST)[-1]
    res, _ = go(w, "the link is not working", tool("send_payment_link", reason="lost"), reply("[no reply]"), live=True)
    assert res.sent == ["pay"] and w.wa.texts(GUEST)[-1] == link


def test_link_lost_after_the_hold_ran_out_sends_a_new_link_and_never_the_expiry(world):
    w = world
    live_link_world(w)
    w.clock.advance(minutes=30)
    res, _ = go(w, "I lost the link, send again", tool("send_payment_link", reason="lost"),
                reply("Sorry, the hold on your room has expired."), live=True)
    assert "ai" not in res.sent and res.sent[-2:] == ["summary", "pay"]
    assert "http" in w.wa.texts(GUEST)[-1] and not any("expire" in m.lower() for m in w.wa.texts(GUEST))
    assert "Link not working / send the link again / lost the link" in w.llm.calls[-1].system


# --------------------------------------------------------------------------- i) the scenario key


def test_ten_adults_in_four_rooms_expects_the_how_many_key():
    scenarios = yaml.safe_load((ROOT / "tests/eval/agent_scenarios.yaml").read_text(encoding="utf-8"))
    sc = next(s for s in scenarios if s["id"] == "ten_adults_four_rooms_how_many")
    assert sc["turns"][0]["expect_sent"] == ["ask_extra_beds"]


# --------------------------------------------------------------------------- j) the judge sees only the AI's own lines


def test_the_judge_scores_only_the_ais_lines_and_sees_code_lines_as_system(monkeypatch):
    monkeypatch.setenv("RESORT_STAFF_NUMBERS", "+910000000000")  # the eval script sets it on import: restored after
    mod_spec = importlib.util.spec_from_file_location("eval_agent_r2", ROOT / "scripts" / "eval_agent.py")
    ev = importlib.util.module_from_spec(mod_spec)
    monkeypatch.setitem(sys.modules, "eval_agent_r2", ev)  # its dataclasses look the module up there
    monkeypatch.setattr(sys, "path", list(sys.path))  # the script puts the project root first: undone after
    mod_spec.loader.exec_module(ev)
    llm = MockLLM([MockLLM.text('{"score": 5, "reason": "ok"}')])
    log = [{"n": 1, "guest": "pool?", "outcome": "answered", "ai": "Yes, there is a pool.", "photos": 2,
            "texts": ["Yes, there is a pool.", "Please share your name and mobile number to proceed."]},
           {"n": 2, "guest": "Asha 9847011111", "outcome": "answered", "ai": "", "photos": 0,
            "texts": ["Please pay the advance using this link:\nhttps://x"]}]
    assert ev.judge(llm, {"id": "x"}, log) == (5, "ok")
    chat = llm.calls[0].messages[0].content
    assert "BOT: Yes, there is a pool." in chat and "SYSTEM: Please share your name and mobile number" in chat
    assert "BOT: (nothing of its own)" in chat and "SYSTEM: Please pay the advance using this link: https://x" in chat
    assert "SYSTEM: [2 photos]" in chat and chat.count("Yes, there is a pool.") == 1
    assert "NEVER blame the bot" in llm.calls[0].system


# --------------------------------------------------------------------------- k) the old-bot tests


def test_the_five_old_bot_pay_terms_tests_are_skipped_with_the_reason():
    src = (ROOT / "tests/resort/test_never_silent.py").read_text(encoding="utf-8")
    assert '@pytest.mark.skip(reason="old bot not live; pay_terms text changed in 8330e2b")' in src


# --------------------------------------------------------------------------- Part 2: no repeats


def test_the_same_or_nearly_the_same_text_never_goes_twice_in_one_turn():
    box = Outbox(GUEST, dry_run=True)
    box.text("rooms_ok", "Yes, rooms are available for your dates.")
    box.text("pay", "Please pay the advance using this link:\nhttps://x")
    box.text("pay", "Please pay the advance using this link:\nhttps://x")
    box.text("again", "Yes rooms are available for your dates!")
    box.prepend_text("ai", "yes, rooms are AVAILABLE for your dates")
    dropped = box.dedupe()
    assert [i.key for i in box.queue] == ["rooms_ok", "pay"] and len(dropped) == 3


def test_different_texts_all_stay():
    assert not near_same("Yes, there is a pool.", "Yes, there is parking.")
    box = Outbox(GUEST, dry_run=True)
    box.text("rooms_ok", "Yes, rooms are available for your dates.")
    box.prepend_text("ai", "Yes, there is a pool.")
    assert box.dedupe() == [] and len(box.queue) == 2


def test_the_ais_line_equal_to_a_tool_message_is_dropped_in_the_turn(world):
    res, box = go(world, ASK, tool("update_enquiry", **STAY), reply("Yes, rooms are available for your dates."))
    assert "ai" not in res.sent and res.sent.count("rooms_ok") == 1


def test_a_sending_tool_runs_only_once_per_turn(world):
    w = world
    run(w, "update_enquiry", {"place": "Alleppey"})
    res, box = go(w, "complaint", tool("talk_to_owner", reason="other", staff_note="noise"),
                  tool("talk_to_owner", reason="other", staff_note="noise"), reply("[no reply]"))
    assert res.sent == ["handoff", "staff:alert"]
    assert res.calls[1]["result"]["facts"] == {"already_done": True} and res.calls[1]["result"]["note"] == ONCE_NOTE


def test_a_reading_tool_may_run_twice(world):
    res, _ = go(world, "my bookings?", tool("my_bookings"), tool("my_bookings"), reply("You have no booking yet."))
    assert all("already_done" not in c["result"]["facts"] for c in res.calls)


@pytest.mark.parametrize("line", ["I have sent the link again.", "I've shared the photos.", "Resent it.",
                                  "I passed your question to the team.", "I have informed the owner."])
def test_a_sent_claim_is_dropped_when_nothing_was_sent(line):
    assert guard.drop_false_sent(line) == ""
    assert guard.drop_false_sent(line + " Yes, there is a pool.") == "Yes, there is a pool."


def test_a_sent_claim_with_no_sending_tool_is_dropped_in_the_turn(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply("[no reply]"), live=True)
    w.clock.advance(minutes=1)
    res, _ = go(w, "did you send it?", reply("Yes, I have sent it again."), live=True)
    assert "ai" not in res.sent and res.sent == ["ask_contact"]  # never silent: the open step again (never "Sure.")


@pytest.mark.parametrize("line", ["Your link is open for 10 more minutes.", "Please pay within 15 minutes.",
                                  "The hold will end soon.", "Sorry, your link has expired."])
def test_the_hold_or_the_links_expiry_is_never_explained(line):
    assert guard.drop_hold_talk(line) == ""


def test_minutes_that_are_not_about_the_link_stay():
    assert guard.drop_hold_talk("The beach is a 5 minutes walk.") == "The beach is a 5 minutes walk."


# --------------------------------------------------------------------------- Part 3: team wording and policies


@pytest.mark.parametrize("lang", ["en", "mn", "tn", "hn", "ml", "ta", "hi"])
def test_the_handoff_text_in_every_language(lang):
    assert t("handoff", lang, numbers="94978 61924") == "Thank you. Please contact the team directly: 94978 61924."


@pytest.mark.parametrize("key", ["handoff", "ai_down", "owner_will_help", "pay_failed_link"])
@pytest.mark.parametrize("lang", ["en", "mn", "tn", "hn"])
def test_the_agents_guest_texts_say_team_never_owner(key, lang):
    text = t(key, lang, numbers="1")
    assert "owner" not in text.lower() and "team" in text.lower()


@pytest.mark.parametrize("line,said", [("Please contact the owner.", "Please contact the team."),
                                       ("The owner's number is below.", "The team's number is below."),
                                       ("Owner will see it.", "Team will see it.")])
def test_the_ais_own_owner_is_said_as_team(line, said):
    assert guard.team_not_owner(line) == said


def test_the_ais_owner_gets_one_retry(world):  # fix round 3: "owner" is a banned phrase
    res, box = go(world, "who runs it?", reply("The owner runs the resort."), reply("The team runs the resort."))
    assert res.retried and texts(box)["ai"] == "The team runs the resort."


@pytest.mark.parametrize("reason", ["call_request", "other"])  # cannot_answer: the open step follows (fix round 3)
def test_after_a_handoff_only_its_text_goes_no_ai_line_no_link_reminder(world, reason):
    w = world
    live_link_world(w)
    res, _ = go(w, "can I talk to someone?", tool("talk_to_owner", reason=reason),
                reply("I've passed it on. Please pay using the link."), live=True)
    assert [k for k in res.sent if not k.startswith("staff:")] == ["handoff"] and res.reply == ""
    assert ("staff:alert" in res.sent) == (reason == "other")


def test_staff_alerts_stay_for_pay_terms_and_paid_changes(world):
    for reason in ("pay_terms", "change_or_cancel_paid"):
        result, _, _ = run(world, "talk_to_owner", {"reason": reason})
        assert "staff:alert" in result["sent"], reason
    for reason in ("cannot_answer", "call_request", "guest_called"):
        result, _, _ = run(world, "talk_to_owner", {"reason": reason})
        assert "staff:alert" not in result["sent"], reason


def test_the_prompt_has_the_policy_team_and_greeting_rules(world):
    system = system_prompt(world)
    assert ("If the answer is not written in FACTS, you do NOT know it. Call talk_to_owner(cannot_answer). Never answer "
            "policy\n  questions") in system
    assert 'Say "team", never "owner".' in system
    assert "Any greeting, in any words or language (hi, hello, hai, vanakkam, namaste, good morning) → greet." in system
    assert "Never explain the hold or when a link expires." in system
    assert "Never fill month or year yourself" in system


def test_the_enquiry_survives_a_quiet_handoff(world):
    w = world
    go(w, ASK, tool("update_enquiry", **STAY), reply("[no reply]"))
    go(w, "can I call?", tool("talk_to_owner", reason="call_request"), reply("Sure."))
    assert Enquiry.load(w.repo, GUEST).check_in is not None
