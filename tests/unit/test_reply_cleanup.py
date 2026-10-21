"""Reply clean-up rules from the user (2026-10-21): never the customer's name; 24/7, no unasked timings."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.agent import prompts
from app.agent.agent import plain_dashes, without_names, without_titles
from app.core.client_config import load_client_config
from app.core.clock import IST


@pytest.mark.parametrize("reply, expected", [
    ("Hi Danish, Swift undu. Kaananam?", "Hi, Swift undu. Kaananam?"),
    ("Hello danish sir, Creta undu", "Hello sir, Creta undu"),
    ("Danish, ithu nokku: Swift ₹5 lakh", "ithu nokku: Swift ₹5 lakh"),
    ("Thanks, Danish Ali!", "Thanks!"),
    ("Swift VXi 2019 undu sir", "Swift VXi 2019 undu sir"),
])
def test_customer_name_is_removed_from_address_positions(reply, expected) -> None:
    assert without_names(reply, ["Danish Ali", None]) == expected


def test_a_profile_name_that_is_a_car_word_does_not_damage_the_reply() -> None:
    assert without_names("Swift undu, Swift VXi 2019", ["Swift"]) == "Swift undu, Swift VXi 2019"
    assert without_names("Hi, Swift undu", [None, ""]) == "Hi, Swift undu"


@pytest.mark.parametrize("reply, expected", [
    ("Undu sir, 5 lakh il automatic und.", "Undu, 5 lakh il automatic und."),
    ("സാർ, വില കാര്യം team ആണ്.", "വില കാര്യം team ആണ്."),
    ("Hello madam!", "Hello!"),
    ("Desire undu", "Desire undu"),
])
def test_sir_and_madam_are_removed(reply, expected) -> None:
    assert without_titles(reply) == expected


def test_odd_hyphens_in_car_ids_become_plain() -> None:
    assert plain_dashes("TC‑2253AD · ₹5–6 lakh") == "TC-2253AD · ₹5–6 lakh"


def test_turn_context_says_24x7_and_hours_only_when_asked(clients_dir) -> None:
    config = load_client_config("truechoice", clients_dir)
    night = datetime(2026, 10, 16, 23, 0, tzinfo=IST)
    text = prompts.turn_context(config, language="en", now=night, customer_name="Danish", source="text")
    assert "You answer 24/7" in text and "ONLY if the customer asks about timings" in text
    assert "Danish" not in text and "no sir/madam" in text
