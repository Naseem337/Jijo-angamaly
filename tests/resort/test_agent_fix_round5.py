"""Agent fix round 5 (booking talk to tools, shorter lines, no re-sent steps, language, dates, all_rooms, owner rules),
with the FAKE LLM."""

from __future__ import annotations

import pytest
from test_agent_fix_round3 import eval_module, spec
from test_agent_photos import keys, pw  # noqa: F401  (pw is a fixture)
from test_agent_photos import run as prun
from test_agent_turn import ASK, STAY, go, texts
from test_bot import GUEST, reply, tool, world  # noqa: F401  (world is a fixture)

from app.resort.agent import guard
from app.resort.agent.prompt import SYSTEM
from app.resort.agent.tools import TOOL_SCHEMAS
from app.resort.i18n import t
from app.resort.models import RoomType

NAMES = ["pozhiyoram", "alleppey", "kanthalloor", "munnar"]


def book_live(w):
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    w.clock.advance(minutes=1)
    go(w, "Asha, same number", tool("create_booking", guest_name="Asha", mobile="same"), reply(""), live=True)
    w.clock.advance(minutes=1)


# --------------------------------------------------------------------------- 1. booking messages go to update_enquiry


def test_how_you_talk_keeps_only_non_booking_examples():
    examples = SYSTEM[SYSTEM.index("Examples (non-booking questions only):"):]
    for q in ("is there a pool?", "is there parking?", "is it secure?", "what's the weather in december?"):
        assert f"Guest: {q}" in examples
    assert "kochi" not in SYSTEM.lower() and "we have properties" not in SYSTEM


@pytest.mark.parametrize("line", ["Sorry sir, we have properties only in Alleppey and Munnar.",
                                  "Sorry sir, Vibes Kanthalloor is currently not available.",
                                  "Hi sir, we have a beachfront property in Alleppey called Pozhiyoram Resort.",
                                  "Which resort would you like, sir?", "Alleppey or Munnar?"])
def test_booking_talk_is_found(line):
    assert guard.booking_talk(line, NAMES)


@pytest.mark.parametrize("line", ["December in Alleppey is cool and dry.", "Yes sir, there is a pool.",
                                  "Yes sir, it's a secure online payment link."])
def test_non_booking_answers_are_not_booking_talk(line):
    assert not guard.booking_talk(line, NAMES)


def test_booking_talk_gets_one_retry_and_the_tool_answers(world):
    res, box = go(world, "I am looking for a property in kochi",
                  reply("Sorry sir, we have properties only in Alleppey and Munnar."),
                  tool("update_enquiry", place="Kochi", language="en"), reply(""))
    assert res.retried and guard.BOOKING_RETRY in res.guard
    assert guard.BOOKING_RETRY in world.llm.calls[1].messages[-1].content
    assert res.sent[0] == "not_our_place" and "ai" not in res.sent


def test_booking_talk_still_without_the_tool_is_dropped_and_the_code_asks(world):
    line = "Sorry sir, we have properties only in Alleppey and Munnar."
    res, box = go(world, "I am looking for a property in kochi", reply(line), reply(line))
    assert res.retried and "ai" not in res.sent and res.sent == ["ask_location"]


def test_a_weather_answer_naming_the_town_is_not_retried(world):
    res, box = go(world, "weather in alleppey in december?", reply("December in Alleppey is cool and dry."))
    assert not res.retried and texts(box)["ai"] == "December in Alleppey is cool and dry."


# --------------------------------------------------------------------------- 2. shorter AI lines


def test_the_limit_is_one_sentence_and_twenty_words():
    assert (guard.MAX_SENTENCES, guard.MAX_WORDS) == (1, 20)
    assert guard.TOO_LONG == "Say it in one short sentence, under 15 words."
    assert guard.too_long("Yes, there is a pool. It is open daily.")
    assert not guard.too_long("Yes sir, there is a pool.")


def test_two_sentences_get_the_short_retry_then_the_first_sentence(world):
    res, box = go(world, "is there a pool?", reply("Yes, there is a pool. It is open daily."),
                  reply("Yes, there is a pool. Towels are given."))
    assert res.retried and guard.TOO_LONG in world.llm.calls[-1].messages[-1].content
    assert texts(box)["ai"] == "Yes, there is a pool."


@pytest.mark.parametrize("line", ["Sure sir, here are the details.", "Photos sent above, sir.",
                                  "For 2 persons you get 1 room."])
def test_a_line_about_what_the_tools_sent_is_dropped(world, line):
    res, box = go(world, ASK, tool("update_enquiry", **STAY), reply(line))
    assert "ai" not in res.sent and not res.retried


def test_a_line_answering_something_else_stays_after_tools(world):
    res, box = go(world, ASK + ", is there a pool?", tool("update_enquiry", **STAY), reply("Yes, there is a pool."))
    assert texts(box)["ai"] == "Yes, there is a pool."


def test_the_prompt_says_answer_only_what_was_asked():
    assert "Answer only what was asked. No extra selling points unless the guest asks about the resort." in SYSTEM


# --------------------------------------------------------------------------- 3. no re-sent steps after ok / thanks


def test_ok_after_the_name_ask_gets_sure_sir_then_nothing(world):
    go(world, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    world.clock.advance(minutes=1)
    res, _ = go(world, "ok", reply(""), live=True)
    assert res.sent == ["sure"] and world.wa.texts(GUEST)[-1] == "Sure sir."
    world.clock.advance(minutes=1)
    res, _ = go(world, "ok", reply(""), live=True)
    assert res.sent == [] and world.wa.texts(GUEST).count(t("ask_contact", "en")) == 1


def test_thanks_right_after_the_link_gets_only_sure_sir(world):
    book_live(world)
    res, _ = go(world, "thanks", reply(""), live=True)
    assert res.sent == ["sure"]


def test_thanks_later_with_an_open_link_gets_the_pay_nudge_once(world):
    book_live(world)
    go(world, "is there wifi?", reply("Yes, there is wifi."), live=True)
    go(world, "and a fridge?", reply("Yes, there is a fridge."), live=True)
    res, _ = go(world, "ok thanks", reply(""), live=True)
    assert res.sent == ["pay_nudge"]
    assert world.wa.texts(GUEST)[-1] == "Please complete the advance payment to confirm your booking."
    res, _ = go(world, "ok", reply(""), live=True)
    assert res.sent == ["sure"]
    res, _ = go(world, "ok", reply(""), live=True)
    assert res.sent == []


def test_an_answer_does_not_resend_a_step_among_the_last_two_messages(world):
    go(world, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    res, _ = go(world, "is there a pool?", reply("Yes, there is a pool."), live=True)
    assert res.sent == ["ai"]


def test_an_older_step_comes_back_after_an_answer(world):
    go(world, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    go(world, "is there a pool?", reply("Yes, there is a pool."), live=True)
    go(world, "and wifi?", reply("Yes, there is wifi."), live=True)
    res, _ = go(world, "and a fridge?", reply("Yes, there is a fridge."), live=True)
    assert res.sent == ["ai", "ask_contact"]  # the name ask is now 3 messages back


def test_after_a_handoff_the_step_still_follows(world):  # a fixed text spoke this turn (round 3 rule kept)
    go(world, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    res, _ = go(world, "parking?", tool("talk_to_owner", reason="cannot_answer", language="en"), reply(""), live=True)
    assert res.sent == ["handoff", "ask_contact"]


def test_pay_nudge_and_sure_exist_in_every_language():
    for lang in ("en", "mn", "tn", "hn"):
        assert t("pay_nudge", lang) and t("sure", lang)


# --------------------------------------------------------------------------- 4. language required


def test_language_is_required_in_every_tool_that_sends_text():
    for s in TOOL_SCHEMAS:
        if "language" in s.parameters["properties"]:
            assert "language" in s.parameters["required"], s.name
    assert {s.name for s in TOOL_SCHEMAS if "language" not in s.parameters["properties"]} == {"my_bookings",
                                                                                            "recall_old_chat"}


def test_a_missing_language_still_falls_back_in_code(world):
    from test_agent_fix_round3 import call
    from app.resort.agent.state import Enquiry
    Enquiry(language="manglish").save(world.repo, GUEST)
    call(world, "update_enquiry", {}, said="room venam")
    assert Enquiry.load(world.repo, GUEST).language == "manglish"


# --------------------------------------------------------------------------- 5. dates


def test_the_date_description_says_a_month_word_is_always_copied():
    desc = spec("update_enquiry").parameters["properties"]["check_in"]["description"]
    assert "'20 Nov' → {day: 20, month: 11}. A month word the guest wrote is ALWAYS copied." in desc
    assert "Only leave month null if the guest wrote no month." in desc


# --------------------------------------------------------------------------- 6. all_rooms when everything was shown


def test_all_rooms_after_everything_was_shown_goes_on_never_no_other_room(pw):  # noqa: F811
    w = pw
    rt = w.repo.get_room_type(w.deluxe.id)
    own = {"108": [w.refs["deluxe"][0]], "109": [w.refs["deluxe"][1]]}
    w.repo.save_room_type(RoomType(**{**rt.__dict__, "room_list": ["108", "109"], "number_photos": own,
                                      "photo_urls": []}))
    prun(w, "update_enquiry", STAY)  # Room 108 shown
    prun(w, "send_photos", {"kind": "all_rooms"})  # the Family Suite
    result, items, _ = prun(w, "send_photos", {"kind": "all_rooms"})
    assert "no_other_room" not in keys(items) and items[0][1] == "*Room 109 - Deluxe*"
    result, items, _ = prun(w, "send_photos", {"kind": "all_rooms"})
    assert keys(items)[0] == "photos_sent"


# --------------------------------------------------------------------------- 7. owner rules in tool descriptions


def test_the_discount_rule_is_in_talk_to_owner():
    desc = spec("talk_to_owner").description
    assert "NOT for the first discount ask. First discount ask → answer yourself in one warm line with a real good " \
           "point from FACTS. Only the SECOND discount ask → talk_to_owner(other)." in desc


def test_the_hold_rule_is_in_send_payment_link():
    desc = spec("send_payment_link").description
    assert ("the guest wants to hold the room, reserve, or pay later/tonight/tomorrow with an open enquiry → "
            "send_payment_link(reason='hold')") in desc


def test_the_eval_checks_the_place_code_resolved(monkeypatch):
    ev = eval_module(monkeypatch)
    calls = [{"name": "update_enquiry", "args": {"place": "kanthaloor"},
              "result": {"facts": {"resort": "Kanthalloor, Munnar"}}}]
    assert ev.check_turn({"expect_args": {"update_enquiry": {"place": "kanthalloor"}}}, "answered", calls, [], []) == []


def test_update_enquiry_reports_the_resort_it_resolved(world):
    from test_agent_fix_round3 import call
    result, _, _ = call(world, "update_enquiry", {"place": "alepey"}, said="alepey")
    assert result["facts"]["resort"] == "Pozhiyoram, Alleppey"
