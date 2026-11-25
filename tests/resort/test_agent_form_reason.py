"""The form's first line says why it is asked (owner, 2026-11-26): update_enquiry(reason=price | availability |
booking). The rest of the form (only the missing lines) is the same. get_price with no stay sends the price form."""

from __future__ import annotations

from test_agent_tools import FORM, agent, update
from test_bot import GUEST, world  # noqa: F401  (world is a fixture)

from app.resort.agent import photos_plan as pp
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import TOOLS, I18N_LANG
from app.resort.i18n import t

PRICE_HEAD = "To check the price, please share these details:"
AVAIL_HEAD = "To check availability, please share these details:"
BODY = FORM.split("\n", 1)[1]


def test_price_reason_changes_only_the_first_line(world):
    result, texts, _ = update(world, {"place": "Alleppey", "reason": "price"})
    assert result["sent"] == ["form"] and texts["form"] == PRICE_HEAD + "\n" + BODY


def test_availability_reason_changes_only_the_first_line(world):
    _, texts, _ = update(world, {"place": "Alleppey", "reason": "availability"})
    assert texts["form"] == AVAIL_HEAD + "\n" + BODY


def test_booking_and_no_reason_keep_the_owners_form(world):
    _, texts, _ = update(world, {"place": "Alleppey", "reason": "booking"})
    assert texts["form"] == FORM
    Enquiry().save(world.repo, GUEST)
    _, texts, _ = update(world, {"place": "Alleppey", "reason": "nonsense"})
    assert texts["form"] == FORM


def test_the_price_form_keeps_only_the_missing_lines(world):
    _, texts, _ = update(world, {"place": "Alleppey", "check_in": "2026-11-10", "adults": 2, "reason": "price"})
    assert texts["form"] == PRICE_HEAD + "\n\nCheck out :\nNumber of rooms:"


def test_every_language_has_the_reason_lines():
    for code in I18N_LANG.values():
        assert t("form_head_price", code) == PRICE_HEAD
        assert t("form_head_availability", code) == AVAIL_HEAD


def test_get_price_with_a_place_but_no_dates_sends_the_price_form(world):
    update(world, {"place": "Alleppey"})
    tools, box = agent(world)
    result = tools.get_price({})
    texts = {i.key: i.text for i in box.queue}
    assert result["sent"] == ["form"] and texts["form"].startswith(PRICE_HEAD)


def test_a_price_form_counts_as_the_form_already_sent(world):
    """step_said: after the price form, the plain booking form is not sent again as the open step."""
    _, _, box = update(world, {"place": "Alleppey", "reason": "price"})
    enq = Enquiry.load(world.repo, GUEST)
    assert pp.step_said(world.bot, GUEST, enq, box)


def test_the_tool_schema_offers_the_three_reasons():
    spec = next(x for x in TOOLS if x.name == "update_enquiry")
    assert spec.parameters["properties"]["reason"]["enum"] == ["booking", "price", "availability"]
