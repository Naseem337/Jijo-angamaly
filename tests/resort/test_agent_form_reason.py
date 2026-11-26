"""The form's first line says why it is asked (owner, 2026-11-26): update_enquiry(reason=price | availability |
booking) comes back as form_reason, with form_lines = only the missing lines. Since the AI-first simplification the
AI writes the form with reply() in the owner's words (prompt); the tool sends nothing."""

from __future__ import annotations

from test_agent_tools import update
from test_bot import world  # noqa: F401  (world is a fixture)

from app.resort.agent.prompt import SYSTEM
from app.resort.agent.tools import TOOLS
from app.resort.i18n import t

ALL_LINES = ["Check in :", "Check out :", "Number of adults (above 6 years) :", "Number of rooms:"]


def test_price_reason_comes_back_with_all_the_lines(world):
    result, texts, _ = update(world, {"place": "Alleppey", "reason": "price"})
    assert result["sent"] == [] and texts == {}
    assert result["facts"]["form_reason"] == "price" and result["facts"]["form_lines"] == ALL_LINES


def test_availability_and_booking_reasons(world):
    result, _, _ = update(world, {"place": "Alleppey", "reason": "availability"})
    assert result["facts"]["form_reason"] == "availability"
    result, _, _ = update(world, {"place": "Alleppey"})
    assert result["facts"]["form_reason"] == "booking"
    result, _, _ = update(world, {"place": "Alleppey", "reason": "nonsense"})
    assert result["facts"]["form_reason"] == "booking"


def test_the_form_lines_are_only_the_missing_ones(world):
    result, _, _ = update(world, {"place": "Alleppey", "check_in": "2026-11-10", "adults": 2, "reason": "price"})
    assert result["facts"]["form_lines"] == ["Check out :", "Number of rooms:"]


def test_get_price_with_a_place_but_no_dates_returns_the_price_reason(world):
    from test_agent_tools import agent
    update(world, {"place": "Alleppey"})
    tools, box = agent(world)
    result = tools.get_price({})
    assert result["facts"]["form_reason"] == "price" and box.queue == []


def test_the_prompt_has_the_three_first_lines_and_the_form_lines_match_i18n():
    assert "Please share these details:" in SYSTEM and "To check the price, please share these details:" in SYSTEM
    assert "To check availability, please share these details:" in SYSTEM
    assert t("form", "en").split("\n")[2:] == ALL_LINES


def test_the_tool_schema_offers_the_three_reasons():
    spec = next(x for x in TOOLS if x.name == "update_enquiry")
    assert spec.parameters["properties"]["reason"]["enum"] == ["booking", "price", "availability"]
