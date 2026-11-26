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


# --------------------------------------------------------------------------- 2. shorter AI lines


# --------------------------------------------------------------------------- 3. no re-sent steps after ok / thanks


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


# --------------------------------------------------------------------------- 7. owner rules in tool descriptions


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
