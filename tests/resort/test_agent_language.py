"""Live bug 2026-11-25: a guest who wrote only English got the owner's texts in Manglish. The reply language is the AI's
judgement of the guest's LATEST message, on every tool call; a missing value falls back only to this conversation's
last turn; 'English please' locks it; restart clears it all. FAKE LLM only."""

from __future__ import annotations

import pytest
from test_agent_gate import agent, m, send  # noqa: F401  (agent is a fixture)
from test_agent_turn import ASK, STAY, go, texts
from test_bot import GUEST, reply, tool, world  # noqa: F401  (world is a fixture)

from app.providers.llm.base import CACHE_SPLIT
from app.resort.agent import language
from app.resort.agent.prompt import SYSTEM
from app.resort.agent.tools import I18N_LANG, TOOL_SCHEMAS
from app.resort.i18n import t


def ask(style: str) -> str:
    return t("ask_contact", I18N_LANG[style])


def manglish_chat(w):
    go(w, ASK, tool("update_enquiry", **STAY, language="manglish"), reply(""), live=True)
    w.clock.advance(minutes=1)


def pad(w):
    """Three AI answers, so the name ask is no longer among our last 3 messages (it may then go again)."""
    for q, a in (("pool?", "Yes, there is a pool."), ("wifi?", "Yes, there is wifi."), ("ac?", "Yes, there is AC.")):
        go(w, q, reply(a), live=True)
        w.clock.advance(minutes=1)


def step_again(w, text, given=None):
    """The guest asks for the step again; the AI passes `given` (None = no language at all)."""
    pad(w)
    args = {} if given is None else {"language": given}
    res, box = go(w, text, tool("repeat_step", **args), reply(""), live=True)
    w.clock.advance(minutes=1)
    return texts(box)


def test_the_owner_texts_really_differ_by_language():
    assert ask("en") != ask("manglish")


# --------------------------------------------------------------------------- 2. the LATEST message decides


def test_prompt_and_every_language_field_say_latest_message_only():
    assert "Reply language = the language of the guest's LATEST message only, never an earlier one" in SYSTEM
    for spec in TOOL_SCHEMAS:
        if field := spec.parameters["properties"].get("language"):
            assert field["description"].startswith(
                "language = the language of the guest's LATEST message only, never an earlier one"), spec.name


def test_a_guest_who_switches_to_english_gets_english_owner_texts(world):
    manglish_chat(world)
    assert world.wa.texts(GUEST)[-1] == ask("manglish")
    assert step_again(world, "send the details again please", "en")["ask_contact"] == ask("en")


def test_the_say_again_fallback_uses_this_conversations_language(world):
    manglish_chat(world)
    res, box = go(world, "hmm", reply(""), live=True)
    assert texts(box)["say_again_short"] == t("say_again_short", I18N_LANG["manglish"])


# --------------------------------------------------------------------------- 3. fallback, same conversation only


@pytest.mark.parametrize("given", [None, "xx", ""])
def test_a_missing_language_falls_back_to_this_conversations_last_turn(world, given):
    manglish_chat(world)
    assert step_again(world, "venda, onnu koode ayakkamo", given)["ask_contact"] == ask("manglish")


def test_a_missing_language_never_comes_from_an_earlier_conversation(world):
    manglish_chat(world)
    world.clock.advance(hours=13)  # a new conversation
    go(world, "send it again", tool("repeat_step"), reply(""), live=True)  # no language passed
    assert world.wa.texts(GUEST)[-1] == ask("en")


def test_the_stored_language_belongs_to_one_conversation(world):
    manglish_chat(world)
    saved = world.repo.get_state(GUEST)[language.STATE_KEY]
    assert saved["style"] == "manglish" and saved["lock"] is None
    assert language.load(world.bot, GUEST, saved["conv"]).base == "manglish"
    assert language.load(world.bot, GUEST, "2000-01-01T00:00:00").base == "en"


# --------------------------------------------------------------------------- 4. 'English please' locks it


def test_english_please_locks_english_for_the_rest_of_the_conversation(world):
    manglish_chat(world)
    res, _ = go(world, "English please", tool("set_language", language="en"), reply("Sure."), live=True)
    assert world.repo.get_state(GUEST)[language.STATE_KEY]["lock"] == "en"
    world.clock.advance(minutes=1)
    assert step_again(world, "athu onnu koode", "manglish")["ask_contact"] == ask("en")  # the lock wins
    go(world, "ok", reply("[ack]"), live=True)
    assert 'The guest asked for English: reply in it and pass language="en"' in \
        world.llm.calls[-1].system.split(CACHE_SPLIT)[-1]


def test_set_language_sends_nothing_itself(world):
    res, _ = go(world, "English please", tool("set_language", language="en"), reply("Sure."), live=True)
    assert res.calls[0]["result"]["ok"] and res.calls[0]["result"]["sent"] == []


# --------------------------------------------------------------------------- 1. restart clears language and lock


def test_restart_clears_the_language_and_the_lock(agent):
    w = agent
    manglish_chat(w)
    go(w, "Malayalam il parayamo", tool("set_language", language="manglish"), reply(""), live=True)
    assert w.repo.get_state(GUEST)[language.STATE_KEY]["lock"] == "manglish"
    assert send(w, m("restart")) == "restart"
    w.clock.advance(minutes=1)
    assert language.STATE_KEY not in w.repo.get_state(GUEST)
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)  # English, the AI passed no language
    assert w.wa.texts(GUEST)[-1] == ask("en")
    assert "LANGUAGE\nThe guest asked" not in w.llm.calls[-1].system
