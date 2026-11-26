"""Live bug 2026-11-25: a guest who wrote only English got the owner's texts in Manglish. The reply language is the AI's
judgement of the guest's LATEST message, on every tool call; a missing value falls back only to this conversation's
last turn; 'English please' locks it; restart clears it all. FAKE LLM only."""

from __future__ import annotations

import pytest
from test_agent_gate import agent, m, send  # noqa: F401  (agent is a fixture)
from test_agent_turn import ASK, STAY, go, texts
from test_bot import GUEST, answer, reply, tool, world  # noqa: F401  (world is a fixture)

from app.providers.llm.base import CACHE_SPLIT
from app.resort.agent import language
from app.resort.agent.prompt import SYSTEM
from app.resort.agent.tools import I18N_LANG, TOOL_SCHEMAS
from app.resort.i18n import t


def style(w) -> str:
    """The language this conversation's last turn used (what the next turn falls back to)."""
    return w.repo.get_state(GUEST)[language.STATE_KEY]["style"]


def manglish_chat(w):
    go(w, ASK, tool("update_enquiry", **STAY, language="manglish"), reply(""), live=True)
    w.clock.advance(minutes=1)


def test_the_stored_language_belongs_to_one_conversation(world):
    manglish_chat(world)
    saved = world.repo.get_state(GUEST)[language.STATE_KEY]
    assert saved["style"] == "manglish" and saved["lock"] is None
    assert language.load(world.bot, GUEST, saved["conv"]).base == "manglish"
    assert language.load(world.bot, GUEST, "2000-01-01T00:00:00").base == "en"


# --------------------------------------------------------------------------- 4. 'English please' locks it


def test_set_language_sends_nothing_itself(world):
    res, _ = go(world, "English please", tool("set_language", language="en"), answer("Sure."), live=True)
    assert res.calls[0]["result"]["ok"] and res.calls[0]["result"]["sent"] == []


# --------------------------------------------------------------------------- 1. restart clears language and lock




# --------------------------------------------------------------------------- AI-first simplification: reply() carries it


def test_prompt_and_every_language_field_say_latest_message_only():
    assert "LATEST message" in SYSTEM
    for spec in TOOL_SCHEMAS:
        if field := spec.parameters["properties"].get("language"):
            assert field["description"].startswith(
                "language = the language of the guest's LATEST message only, never an earlier one"), spec.name


def test_the_reply_language_is_this_turns_language(world):
    manglish_chat(world)
    go(world, "can you check in english?", answer("Sure, the rooms are free.", language="en"), live=True)
    assert style(world) == "en"


def test_the_say_again_fallback_uses_this_conversations_language(world):
    manglish_chat(world)
    res, box = go(world, "hmm", reply(""), reply(""), live=True)
    assert texts(box)["say_again_short"] == t("say_again_short", I18N_LANG["manglish"])


@pytest.mark.parametrize("given", [None, "xx", ""])
def test_a_missing_language_falls_back_to_this_conversations_last_turn(world, given):
    manglish_chat(world)
    go(world, "venda, onnu koode parayamo", answer("Sheri.", language=given), live=True)
    assert style(world) == "manglish"


def test_a_missing_language_never_comes_from_an_earlier_conversation(world):
    manglish_chat(world)
    world.clock.advance(hours=13)  # a new conversation
    go(world, "hello again", answer("Hi, How may I assist you?"), live=True)  # no language passed
    assert style(world) == "en"


def test_english_please_locks_english_for_the_rest_of_the_conversation(world):
    manglish_chat(world)
    go(world, "English please", tool("set_language", language="en"), answer("Sure."), live=True)
    assert world.repo.get_state(GUEST)[language.STATE_KEY]["lock"] == "en"
    world.clock.advance(minutes=1)
    go(world, "athu onnu koode", answer("Sure, in English.", language="manglish"), live=True)
    assert style(world) == "en"  # the lock wins
    go(world, "ok", answer("Okay 👍"), live=True)
    assert 'The guest asked for English: reply in it and pass language="en"' in \
        world.llm.calls[-1].system.split(CACHE_SPLIT)[-1]


def test_restart_clears_the_language_and_the_lock(agent):
    w = agent
    manglish_chat(w)
    go(w, "Malayalam il parayamo", tool("set_language", language="manglish"), answer("Sheri."), live=True)
    assert w.repo.get_state(GUEST)[language.STATE_KEY]["lock"] == "manglish"
    assert send(w, m("restart")) == "restart"
    w.clock.advance(minutes=1)
    assert language.STATE_KEY not in w.repo.get_state(GUEST)
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)  # English, the AI passed no language
    assert style(w) == "en"
    assert "LANGUAGE\nThe guest asked" not in w.llm.calls[-1].system
