"""Agent fix round 1 (AI-5 eval failures), with the FAKE LLM: the open step comes from code, the AI never repeats what
tools sent, talk_to_owner reasons are an enum with fixed texts, and a day without a month is worked out in code."""

from __future__ import annotations

from datetime import date

import pytest
from test_agent_turn import STAY, go, texts
from test_bot import GUEST, STAFF, reply, tool, world  # noqa: F401  (world is a fixture)

from app.resort.agent import guard
from app.resort.agent.outbox import bot_outbox
from app.resort.agent.prompt import today_block
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import ALREADY_SENT, OWNER_REASONS, TOOL_SCHEMAS, AgentTools
from app.resort.agent.turn import due_step
from app.resort.i18n import t


def run(w, name, args=None, *, live=False):
    box = bot_outbox(w.bot, GUEST, dry_run=not live)
    result = AgentTools(w.bot, GUEST, box).call(name, args or {})
    box.flush()
    return result, {i.key: i.text for i in box.sent}, box


def one_resort(w):
    w.p2.active = False
    w.repo.save_property(w.p2)


# --------------------------------------------------------------------------- 1. the open step comes from code


def test_a_wish_to_book_with_no_details_gets_the_owners_location_question(world):
    res, box = go(world, "I need a room", tool("update_enquiry"), reply("[no reply]"))
    assert res.sent == ["ask_location"] and texts(box)["ask_location"].startswith("Sure, Kindly confirm the Location")


def test_the_ais_own_question_for_the_place_is_dropped_and_the_owner_text_goes(world):
    # fix round 5: asking which place is booking talk -> one retry, still no update_enquiry -> dropped, the code asks
    res, box = go(world, "I need a room", reply("Sure! Which resort or place are you looking for?"),
                  reply("Sure! Which resort or place are you looking for?"))
    assert res.sent == ["ask_location"] and res.retried
    res, box = go(world, "I need a room", reply("Which resort or place are you looking for?"),
                  reply("Which resort or place are you looking for?"))
    assert res.sent == ["ask_location"] and res.outcome == "never_silent"


@pytest.mark.parametrize("line", [
    "Which resort are you looking for?", "Please share your check-in and check-out dates.",
    "How many adults are coming?", "How many rooms do you need?", "Please share your name and mobile number.",
    "Could you tell me your dates?", "Ethra per undu?", "Dates parayamo?", "What is your name?"])
def test_a_sentence_asking_for_booking_details_is_found(line):
    assert guard.asks_details(line)


@pytest.mark.parametrize("line", [
    "Yes, there is a pool.", "Rooms are available for your dates.", "Hi, How may I assist you?",
    "Check-in is at 12 noon.", "You're welcome!", "Please bring your ID proof while coming."])
def test_answers_and_greetings_are_not_detail_asks(line):
    assert not guard.asks_details(line)


def test_only_the_asking_sentence_is_dropped():
    assert guard.drop_detail_asks("Yes, there is a pool. Please share your dates.") == "Yes, there is a pool."
    assert guard.drop_detail_asks("Which dates?") == ""


def test_a_mid_chat_hi_gets_a_short_line_and_the_open_step_from_code(world):
    w = world
    go(w, "Alleppey 10 nov to 12 nov 2 persons", tool("update_enquiry", **STAY), reply("[no reply]"), live=True)
    w.clock.advance(minutes=1)
    res, box = go(w, "hi", reply("Hi sir! To proceed, please share your name and mobile number."),
                  reply("Hi sir! Please share your name and mobile number."), live=True)
    # 'sir' only in the first reply (live test 2026-11-25); the step was the last message and only the AI spoke
    assert res.sent == ["ai"] and texts(box)["ai"] == "Hi!"
    assert w.wa.texts(GUEST)[-2] == t("ask_contact", "en")


def test_the_open_step_follows_an_answer_but_never_twice_in_a_row(world):
    w = world
    go(w, "Alleppey 10 nov to 12 nov 2 persons", tool("update_enquiry", **STAY), reply("[no reply]"), live=True)
    w.clock.advance(minutes=1)
    res, _ = go(w, "is there a pool?", reply("Yes, there is a pool."), live=True)
    assert res.sent == ["ai"]  # fix round 5: the step was among the last 2 messages and only the AI spoke: not re-sent
    enq = Enquiry.load(w.repo, GUEST)
    box = bot_outbox(w.bot, GUEST, dry_run=True)
    assert due_step(w.bot, GUEST, enq, "en", box) is None  # ask_contact is already the last message in the chat


def test_the_open_step_is_not_added_when_its_text_went_this_turn(world):
    res, _ = go(world, "Alleppey 10 nov to 12 nov 2 persons", tool("update_enquiry", **STAY), reply("[no reply]"))
    assert res.sent.count("ask_contact") == 1


def test_a_first_hi_never_gets_a_booking_step(world):
    res, box = go(world, "hi", reply("Hi, How may I assist you?"))
    assert res.sent == ["ai"]


def test_an_open_payment_link_is_sent_again_after_an_answer(world):
    w = world
    go(w, "Alleppey 10 nov to 12 nov 2 persons", tool("update_enquiry", **STAY), reply("[no reply]"), live=True)
    w.clock.advance(minutes=1)
    go(w, "Asha, same number", tool("create_booking", guest_name="Asha", mobile="same"), reply("[no reply]"), live=True)
    link = w.wa.texts(GUEST)[-1]
    assert "http" in link
    w.clock.advance(minutes=1)
    res, _ = go(w, "is there wifi?", reply("Yes, there is wifi."), live=True)
    assert res.sent == ["ai"]  # fix round 5: the step was among the last 2 messages and only the AI spoke: not re-sent
    w.clock.advance(minutes=1)
    res, _ = go(w, "hold it, I pay tonight", tool("send_payment_link", reason="hold"), reply("[no reply]"), live=True)
    assert res.sent == ["hold_pay"]  # the hold text IS the payment step: no second link


def test_one_resort_only_never_asks_the_place(world):
    w = world
    one_resort(w)
    res, box = go(w, "I need a room", tool("update_enquiry"), reply("[no reply]"))
    assert "ask_location" not in res.sent and res.sent == ["form"]
    assert Enquiry.load(w.repo, GUEST).place == w.p1.id


def test_one_resort_only_sends_photos_without_the_location_question(world):
    one_resort(world)
    result, sent, _ = run(world, "send_photos", {"kind": "resort"})
    assert "ask_location" not in result["sent"] and result.get("facts", {}).get("waiting_for") != "location"


# --------------------------------------------------------------------------- 2. no repeats of what tools sent


@pytest.mark.parametrize("line", [
    "The total is ₹8,000.", "Here is the link https://x.y/z", "Your stay is 10 Nov to 12 Nov.",
    "Your summary was sent.", "Please pay the advance.", " ".join(["word"] * 26)])
def test_a_line_that_repeats_the_tools_is_found(line):
    assert guard.repeats_tools(line)


def test_a_short_extra_answer_is_not_a_repeat():
    assert not guard.repeats_tools("Yes, there is a pool.")


def test_the_ais_line_is_dropped_when_it_repeats_the_tools_messages(world):
    res, box = go(world, "Alleppey 10 nov to 12 nov 2 persons", tool("update_enquiry", **STAY),
                  reply("Rooms are available from 10 Nov to 12 Nov. Please share your name and mobile."))
    assert "ai" not in res.sent and not res.retried and res.sent == ["rooms_ok", "rooms_plain", "room_label", "ask_contact"]


def test_a_short_answer_to_another_question_stays_after_the_tools(world):
    res, box = go(world, "Alleppey 10 nov to 12 nov 2 persons, pool?", tool("update_enquiry", **STAY),
                  reply("Yes, there is a pool."))
    assert res.sent[0] == "ai" and texts(box)["ai"] == "Yes, there is a pool."


def test_every_tool_that_sent_something_says_already_sent_first(world):
    for name, args in (("update_enquiry", {}), ("update_enquiry", STAY), ("get_price", {}),
                       ("talk_to_owner", {"reason": "cannot_answer"})):
        result, _, _ = run(world, name, args)
        assert result["sent"] and result["note"].startswith(ALREADY_SENT), name
    result, _, _ = run(world, "my_bookings")
    assert not result["sent"] and not result["note"].startswith(ALREADY_SENT)


# --------------------------------------------------------------------------- 3. talk_to_owner reasons


def test_talk_to_owner_takes_only_the_six_reasons():
    spec = next(s for s in TOOL_SCHEMAS if s.name == "talk_to_owner")
    assert spec.parameters["properties"]["reason"]["enum"] == ["pay_terms", "guest_called", "cannot_answer",
                                                               "call_request", "change_or_cancel_paid", "other"]
    assert "staff_note" in spec.parameters["properties"] and spec.parameters["required"] == ["reason", "language"]
    assert list(OWNER_REASONS) == spec.parameters["properties"]["reason"]["enum"]


@pytest.mark.parametrize("reason,key", [("pay_terms", "pay_terms"), ("change_or_cancel_paid", "handoff"),
                                        ("other", "handoff")])
def test_each_reason_sends_its_fixed_text_and_tells_the_team(world, reason, key):
    run(world, "update_enquiry", {"place": "Alleppey"})
    result, sent, box = run(world, "talk_to_owner", {"reason": reason, "staff_note": "asks something"})
    assert result["sent"] == [key, "staff:alert"]
    staff = next(i for i in box.sent if i.kind == "staff")
    assert "asks something" in staff.text and STAFF in staff.to
    assert result["note"].startswith(ALREADY_SENT) and "Write NOTHING" in result["note"]


@pytest.mark.parametrize("reason", ["cannot_answer", "call_request"])
def test_cannot_answer_and_call_request_send_the_handoff_with_no_staff_alert(world, reason):  # fix round 2
    run(world, "update_enquiry", {"place": "Alleppey"})
    result, sent, box = run(world, "talk_to_owner", {"reason": reason, "staff_note": "asks something"})
    assert result["sent"] == ["handoff"] and not [i for i in box.sent if i.kind == "staff"]
    assert result["note"].startswith(ALREADY_SENT) and "Write NOTHING" in result["note"]


def test_guest_called_thanks_alerts_no_one_and_the_open_step_follows(world):
    run(world, "update_enquiry", STAY)
    result, sent, box = run(world, "talk_to_owner", {"reason": "guest_called"})
    assert result["sent"] == ["guest_called", "ask_contact"] and not [i for i in box.sent if i.kind == "staff"]


def test_a_free_text_reason_is_other_with_the_text_for_the_staff(world):
    result, sent, box = run(world, "talk_to_owner", {"reason": "wants a refund"})
    staff = next(i for i in box.sent if i.kind == "staff")
    assert result["sent"] == ["handoff", "staff:alert"] and "wants a refund" in staff.text
    assert result["facts"]["reason"] == "other"


def test_the_prompt_owner_rules_name_the_reasons(world):
    go(world, "hi", reply("Hi, How may I assist you?"))
    system = world.llm.calls[0].system
    for value in OWNER_REASONS:
        assert f'reason="{value}"' in system or value in system, value
    assert "Never ask for place, dates, adults, rooms, name or mobile yourself" in system
    assert "If tools sent messages this turn, write NOTHING" in system
    assert "even with no details at all" in system


# --------------------------------------------------------------------------- 4. dates in code


def late_month(w):
    w.clock.advance(days=23)  # 2026-11-25, a Wednesday (the eval's day)


def test_a_day_without_a_month_that_passed_moves_to_next_month_with_its_check_out(world):
    late_month(world)
    result, sent, _ = run(world, "update_enquiry", {"place": "Alleppey", "check_in": {"day": 12},
                                                    "check_out": {"day": 14}, "adults": 2})
    assert result["facts"]["check_in"] == "2026-12-12" and result["facts"]["check_out"] == "2026-12-14"
    assert "past_date" not in result["sent"]


def test_a_day_without_a_month_moves_with_nights_too(world):
    late_month(world)
    result, _, _ = run(world, "update_enquiry", {"place": "Alleppey", "check_in": {"day": 20}, "nights": 3,
                                                 "adults": 2})
    assert result["facts"]["check_in"] == "2026-12-20" and result["facts"]["check_out"] == "2026-12-23"


def test_a_past_date_with_its_month_given_is_told_as_past(world):
    late_month(world)
    result, sent, _ = run(world, "update_enquiry", {"place": "Alleppey",
                                                    "check_in": {"day": 20, "month": 11, "year": 2026},
                                                    "check_out": {"day": 21}, "adults": 2})
    assert result["sent"] == ["past_date"]


def test_a_day_february_does_not_have_skips_to_march(world):
    world.clock.set(world.clock.now().replace(year=2027, month=1, day=31))
    result, _, _ = run(world, "update_enquiry", {"place": "Alleppey", "check_in": {"day": 30}, "nights": 1,
                                                 "adults": 2})
    assert result["facts"]["check_in"] == "2027-03-30"  # no 30 Feb


def test_check_out_or_nights_is_said_in_the_tool_description():
    spec = next(s for s in TOOL_SCHEMAS if s.name == "update_enquiry")
    assert "Give check_out OR nights" in spec.description and "month_given" not in spec.parameters["properties"]


def test_the_today_block_gives_a_worked_example():
    block = today_block(date(2026, 11, 25))
    assert block.startswith("TODAY\nToday is Wed 25 Nov 2026 (2026-11-25), India time.")
    assert "Dates: pass only what the guest said ('12' → {day: 12}); code takes the next time it comes (12 Dec)." in block
    assert "(12 Jan)" in today_block(date(2026, 12, 25))
