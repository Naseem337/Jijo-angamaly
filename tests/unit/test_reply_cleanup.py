"""Reply clean-up rules from the user (2026-10-21): never the customer's name; 24/7, no unasked timings."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.agent import prompts
from app.agent.agent import plain_dashes, without_names
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


def test_odd_hyphens_in_car_ids_become_plain() -> None:
    assert plain_dashes("TC‑2253AD · ₹5–6 lakh") == "TC-2253AD · ₹5–6 lakh"


def test_turn_context_says_24x7_and_hours_only_when_asked(clients_dir) -> None:
    config = load_client_config("truechoice", clients_dir)
    night = datetime(2026, 10, 16, 23, 0, tzinfo=IST)
    text = prompts.turn_context(config, language="en", now=night, customer_name=None, source="text",
                                profile_name="Danish")
    assert "You answer 24/7" in text and "ONLY if the customer asks about timings" in text
    assert "only to tell sir from madam" in text and "NEVER write their name" in text


@pytest.mark.parametrize("reply, hedged", [
    ("Yeah, I guess it's a new car", True),
    ("Probably a 2019 model", True),
    ("ഒരുപക്ഷേ പുതിയതാകാം", True),
    ("It's a 2019 Swift, ₹5.5 lakh (TC-1E6434), pre-owned", False),
    ("Our salesman will contact you to confirm", False),
])
def test_guessing_words_trigger_a_rewrite(reply, hedged) -> None:
    from app.agent.agent import hedge_problem
    assert bool(hedge_problem(reply)) == hedged


def test_prompt_forbids_guessing_and_says_every_car_is_pre_owned(clients_dir) -> None:
    config = load_client_config("truechoice", clients_dir)
    system = prompts.build_system_prompt(config)
    assert "NEVER GUESS" in system and "PRE-OWNED" in system and "salesman will contact" in system


@pytest.mark.parametrize("reply, expected", [
    ("Undu sir, 5 lakh il automatic und.", "Undu, 5 lakh il automatic und."),
    ("സാർ, വില കാര്യം team ആണ്.", "വില കാര്യം team ആണ്."),
    ("Hello madam!", "Hello!"),
    ("Desire undu", "Desire undu"),
])
def test_titles_are_removed_when_nothing_tells_the_gender(reply, expected) -> None:
    from app.agent.agent import without_titles
    assert without_titles(reply) == expected


def test_tool_messages_become_plain_text_when_a_call_has_no_tools() -> None:
    """Sarvam rejects tool messages without tools; the guardrail rewrite sends none (live 2026-10-25)."""
    from app.providers.llm import ChatMessage, ToolCall
    from app.providers.llm.openai_compatible import _to_wire

    call = ToolCall("c1", "search_cars", {"model": "swift"}, None, None)
    messages = [ChatMessage.user("Swift undo?"), ChatMessage("assistant", "", tool_calls=[call]),
                ChatMessage.tool_result("c1", '{"cars": []}'), ChatMessage("assistant", "Illa."),
                ChatMessage.user("rewrite")]
    plain = _to_wire("system", messages, keep_tools=False)
    assert [m["role"] for m in plain] == ["system", "user", "user", "assistant", "user"]
    assert all("tool_calls" not in m and m["role"] != "tool" for m in plain)
    assert "stock tool result" in plain[2]["content"] and '"cars"' in plain[2]["content"]
    assert [m["role"] for m in _to_wire("system", messages)] == ["system", "user", "assistant", "tool", "assistant", "user"]
