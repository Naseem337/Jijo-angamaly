"""Live test on the test number (2026-11-25): never silent, the open step on unclear asks, photo order, 'sir' only in the
first reply, no offers. FAKE LLM only."""

from __future__ import annotations

import pytest
from test_agent_photos import pw  # noqa: F401  (pw is a fixture)
from test_agent_turn import ASK, STAY, go, texts
from test_bot import GUEST, reply, tool, world  # noqa: F401  (world is a fixture)

from app.resort.agent import guard
from app.resort.agent.outbox import Outbox, photo_gap
from app.resort.agent.prompt import SYSTEM
from app.resort.agent.tools import TOOL_SCHEMAS
from app.resort.agent.turn import SECOND_CHANCE, STEP_TEXT_KEYS
from app.resort.agent.state import Enquiry
from app.resort.i18n import t

ASK_CONTACT = "Please share your name and mobile number to proceed."


def checked(w):
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)  # the name ask is now the last message
    w.clock.advance(minutes=1)


def linked(w):
    checked(w)
    go(w, "Asha, same number", tool("create_booking", guest_name="Asha", mobile="same"), reply(""), live=True)
    w.clock.advance(minutes=1)


# --------------------------------------------------------------------------- 1. never silent


def test_every_guard_drops_the_line_then_say_again_never_the_step_again(world):  # AI-first rebalance 2026-11-25
    checked(world)
    res, _ = go(world, "what sure", reply("Sure, I have sent it again."), reply(""), live=True)  # false 'sent': dropped
    assert res.outcome == "never_silent" and res.sent == ["say_again_short"]
    assert world.wa.texts(GUEST).count(ASK_CONTACT) == 1


def test_the_ai_is_asked_once_more_internally_and_its_line_goes(world):
    checked(world)
    res, _ = go(world, "hmm ok", reply(""), reply("Got it, take your time."), live=True)
    assert res.sent == ["ai"] and world.wa.texts(GUEST)[-1] == "Got it, take your time."
    assert SECOND_CHANCE in world.llm.calls[-1].messages[-1].content  # the internal ask ...
    assert all(SECOND_CHANCE not in x for x in world.wa.texts(GUEST))  # ... never reaches the guest


def test_the_second_ask_says_exactly_what_was_asked():
    assert SECOND_CHANCE.endswith("You sent nothing and the guest is waiting. Reply to the guest's last message in one "
                                  "short sentence. Call a tool ONLY if their message clearly needs one (e.g. they gave "
                                  "details or asked for photos/link).")


def test_the_second_ask_may_call_a_tool(world):
    linked(world)
    res, _ = go(world, "send the link", reply(""), tool("repeat_step", language="en"), reply(""), live=True)
    assert res.sent == ["pay"]  # the link the guest asked for may go again


def test_a_step_text_from_the_second_ask_still_obeys_the_last_3_rule(world):
    checked(world)
    res, _ = go(world, "send", reply(""), tool("repeat_step", language="en"), reply(""), live=True)
    assert res.sent == ["say_again_short"] and world.wa.texts(GUEST).count(ASK_CONTACT) == 1


def test_nothing_open_and_nothing_said_gets_say_that_again(world):
    res, box = go(world, "hmm", reply(""))
    assert res.sent == ["say_again_short"] and texts(box)["say_again_short"] == "Sorry, could you say that again?"


def test_an_open_link_is_never_the_fallback(world):  # AI-first rebalance: never a fixed text as the fallback
    linked(world)
    res, _ = go(world, "send", reply(""), reply(""), live=True)
    assert res.sent == ["say_again_short"]


# --------------------------------------------------------------------------- 2. unclear asks re-send the open step


def test_repeat_step_never_resends_a_name_ask_among_the_last_3_messages(world):
    checked(world)
    res, _ = go(world, "ok send me", tool("repeat_step", language="en"), reply("Please send your name and mobile."),
                live=True)
    assert res.sent == ["ai"] and world.wa.texts(GUEST).count(ASK_CONTACT) == 1


def test_repeat_step_resends_the_payment_link(world):
    linked(world)
    res, _ = go(world, "send", tool("repeat_step", language="en"), reply(""), live=True)
    assert res.sent == ["pay"]


def test_repeat_step_resends_the_form(world):
    Enquiry(place=world.p1.id, started=True).save(world.repo, GUEST)
    res, box = go(world, "what", tool("repeat_step", language="en"), reply(""))
    assert res.sent == ["form"]


def test_repeat_step_with_nothing_open_sends_nothing_and_says_so(world):
    res, box = go(world, "send", tool("repeat_step", language="en"), reply("What should I send, please?"))
    assert not res.calls[0]["result"]["ok"] and res.sent == ["ai"]


def test_sure_only_for_an_acknowledgement_with_no_step_open(world):
    go(world, "is there a pool?", reply("Yes, there is a pool."), live=True)
    res, _ = go(world, "ok", reply("[ack]"), live=True)
    assert res.sent == ["sure"] and world.wa.texts(GUEST)[-1] == "Sure."


def test_the_prompt_and_tool_say_when_to_repeat_the_step():
    assert 'The guest clearly asks you to SEND a step again ("send the link again", "send the form again") → '         'repeat_step.' in SYSTEM
    spec = next(s for s in TOOL_SCHEMAS if s.name == "repeat_step")
    assert "NOT for 'what' / an unclear message" in spec.description and spec.parameters["required"] == ["language"]


# --------------------------------------------------------------------------- 3. photo order


def test_a_text_after_photos_waits_and_every_send_returns_first():
    log = []
    box = Outbox("+91", lambda to, x: log.append(("text", x)), lambda to, refs: log.append(("photos", tuple(refs))),
                 photo_gap_s=1.0, sleep=lambda s: log.append(("wait", s)))
    box.text("room_label", "*Room 101 - Deluxe*")
    box.photos("photos:2", ["a", "b"])
    box.text("ask_contact", ASK_CONTACT)
    box.flush()
    assert log == [("text", "*Room 101 - Deluxe*"), ("photos", ("a", "b")), ("wait", 1.0), ("text", ASK_CONTACT)]


def test_the_wait_is_one_second_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_PHOTO_GAP_S", raising=False)
    assert photo_gap() == 1.0
    monkeypatch.setenv("AGENT_PHOTO_GAP_S", "0")
    assert photo_gap() == 0.0


def test_the_open_step_always_goes_last(world):
    res, box = go(world, ASK + ", price?", tool("update_enquiry", **STAY), tool("get_price"), reply(""))
    guest = [k for k in res.sent if not k.startswith("staff:")]
    assert guest[-1] == "ask_contact" and guest.index("price_answer") < guest.index("ask_contact")


def test_ask_contact_is_the_last_message_of_the_room_check_with_photos(pw):  # noqa: F811
    res, box = go(pw, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    assert res.sent[-1] == "ask_contact" and res.sent.index("photos:2") < res.sent.index("ask_contact")
    assert "ask_contact" in STEP_TEXT_KEYS and "pay" in STEP_TEXT_KEYS


# --------------------------------------------------------------------------- 4. 'sir' only in the first reply


def test_a_six_turn_chat_has_sir_only_in_the_first_ai_reply(world):
    lines = ["Yes sir, there is a pool.", "Yes sir, there is wifi.", "Sorry sir, no gym.", "Thank you, sir.",
             "Sir, breakfast is served.", "Okay madam, noted."]
    for n, line in enumerate(lines):
        go(world, f"question {n}?", reply(line), live=True)
        world.clock.advance(minutes=1)
    sent = world.wa.texts(GUEST)
    assert sent == ["Yes sir, there is a pool.", "Yes, there is wifi.", "Sorry, no gym.", "Thank you.",
                    "Breakfast is served.", "Okay, noted."]
    assert [x for x in sent if "sir" in x.lower() or "madam" in x.lower()] == [sent[0]]


@pytest.mark.parametrize("line,fixed", [("Yes sir, there is a pool.", "Yes, there is a pool."),
                                        ("Thank you, sir.", "Thank you."), ("Hi sir.", "Hi."),
                                        ("Sir, the pool is open.", "The pool is open."),
                                        ("Sure madam, done. Thanks sir!", "Sure, done. Thanks!")])
def test_drop_sir_fixes_the_sentence(line, fixed):
    assert guard.drop_sir(line) == fixed


def test_code_fallbacks_never_say_sir_and_owner_texts_stay_word_for_word():
    for lang in ("en", "mn", "tn", "hn"):
        assert "sir" not in t("sure", lang).lower() and "sir" not in t("say_again_short", lang).lower()
    assert t("sure", "en") == "Sure." and t("say_again_short", "en") == "Sorry, could you say that again?"
    assert t("guest_called", "en") == "Thank you sir. Please let me know if you need any help with your booking."
    assert guard.MID_CHAT_HI == "Hi."


def test_the_prompt_has_one_sir_rule():
    assert "Say 'sir' (or 'madam') only in your very first reply of the chat. Never again after that." in SYSTEM
    assert SYSTEM.lower().count("sir") == 1


# --------------------------------------------------------------------------- 5. no extra offers


@pytest.mark.parametrize("phrase", ["let me know if", "if you want to see", "I can also", "would you like"])
def test_offers_are_banned(phrase):
    assert guard.banned_in(f"Sure. {phrase.capitalize()} something.")


def test_an_offer_gets_one_retry_then_its_sentence_goes(world):
    res, box = go(world, "is there a pool?", reply("Yes, there is a pool. Let me know if you want to see other rooms."),
                  reply("Yes, there is a pool, would you like to see it?"))
    assert res.retried and "ai" not in res.sent  # the only sentence had an offer: dropped (never silent fills in)
