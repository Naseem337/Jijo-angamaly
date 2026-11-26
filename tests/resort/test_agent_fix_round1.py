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


def test_one_resort_only_sends_photos_without_the_location_question(world):
    one_resort(world)
    result, sent, _ = run(world, "send_photos", {"kind": "resort"})
    assert "ask_location" not in result["sent"] and result.get("facts", {}).get("waiting_for") != "location"


# --------------------------------------------------------------------------- 2. no repeats of what tools sent


# --------------------------------------------------------------------------- 3. talk_to_owner reasons


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
