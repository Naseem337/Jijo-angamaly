"""AI-first rebalance (live WhatsApp 2026-11-25: the same owner text went 4 times in a row to "What", "Can u speak in
english", "English please"). Owner texts go once, when a step starts; after that the AI talks in its own words; never a
text that is among our last 3 messages; never silent = the AI once more, then 'say that again'. FAKE LLM only."""

from __future__ import annotations

from test_agent_fix_round5 import book_live
from test_agent_turn import ASK, STAY, go, texts
from test_bot import GUEST, reply, tool, world  # noqa: F401  (world is a fixture)

from app.resort.agent import guard
from app.resort.agent import turn as turn_module
from app.resort.agent.outbox import bot_outbox
from app.resort.agent.prompt import SYSTEM
from app.resort.agent.tools import I18N_LANG
from app.resort.i18n import t

ASK_EN = t("ask_contact", "en")
ASK_ML = t("ask_contact", I18N_LANG["manglish"])


def asked(w, language="en"):
    go(w, ASK, tool("update_enquiry", **STAY, language=language), reply(""), live=True)
    w.clock.advance(minutes=1)


def turn(w, text, *script):
    res, box = go(w, text, *script, live=True)
    w.clock.advance(minutes=1)
    return res, box


# --------------------------------------------------------------------------- the live chat, replayed


def test_the_live_chat_never_repeats_the_owner_text(world):
    w = world
    asked(w, "manglish")
    turn(w, "What", reply("Booking cheyyan ningalude name-um mobile number-um venam."))
    res, box = turn(w, "Can u speak in english", tool("set_language", language="en"),
                    reply("Sure, please send your name and mobile number to continue."))
    assert texts(box)["ai"] == "Sure, please send your name and mobile number to continue."
    turn(w, "English please", tool("set_language", language="en"), reply("Of course, I'll reply in English."))
    sent = w.wa.texts(GUEST)
    assert sent.count(ASK_ML) == 1 and ASK_EN not in sent
    assert all(a != b for a, b in zip(sent, sent[1:]))  # never the same text twice in a row


# --------------------------------------------------------------------------- 1. owner texts only when a step starts


def test_an_answer_with_a_step_open_is_only_the_ais_line(world):
    asked(world)
    res, _ = turn(world, "is there a pool?", reply("Yes, there is a pool."))
    assert res.sent == ["ai"]


def test_what_gets_the_ais_own_short_explanation(world):
    asked(world)
    line = "I need your name and mobile number to make the booking."
    res, box = turn(world, "What", reply(line))
    assert res.sent == ["ai"] and texts(box)["ai"] == line


def test_a_changed_stay_sends_the_owner_texts_again(world):
    asked(world)
    res, _ = turn(world, "make it 12 to 14 nov", tool("update_enquiry", **{**STAY, "check_in": "2026-11-12",
                                                                           "check_out": "2026-11-14"}), reply(""))
    assert "rooms_ok" in res.sent  # the step changed: the room check's owner text goes


# --------------------------------------------------------------------------- 2. the removed code behaviours


def test_the_code_no_longer_has_the_step_append_or_the_detail_guard():
    for gone in ("due_step", "quiet_reply", "open_step_now"):
        assert not hasattr(turn_module, gone), gone
    assert not hasattr(guard, "drop_detail_asks") and not hasattr(guard, "asks_details")


def test_the_ais_ask_for_details_is_kept(world):
    asked(world)
    res, box = turn(world, "ok so what next", reply("Please send your name and mobile number to continue."))
    assert res.sent == ["ai"]


# --------------------------------------------------------------------------- 3. never silent


def test_an_empty_reply_gets_the_ai_once_more(world):
    asked(world)
    res, _ = turn(world, "hmm", reply(""), reply("Take your time."))
    assert res.sent == ["ai"] and res.outcome == "never_silent"


def test_still_empty_gets_say_again_in_the_guests_language(world):
    asked(world, "manglish")
    res, box = turn(world, "hmm", reply(""), reply(""))
    assert res.sent == ["say_again_short"] and texts(box)["say_again_short"] == t("say_again_short", I18N_LANG["manglish"])
    assert world.wa.texts(GUEST).count(ASK_ML) == 1


# --------------------------------------------------------------------------- 4. the last-3 hard rule


def test_an_owner_text_among_the_last_3_is_never_sent_again(world):
    asked(world)
    res, _ = turn(world, "send", tool("repeat_step", language="en"), reply("Please send your name and mobile."))
    assert "ask_contact" not in res.sent and world.wa.texts(GUEST).count(ASK_EN) == 1


def test_an_owner_text_older_than_the_last_3_may_go_again(world):
    asked(world)
    for q, a in (("pool?", "Yes, there is a pool."), ("wifi?", "Yes, there is wifi."), ("ac?", "Yes, there is AC.")):
        turn(world, q, reply(a))
    res, _ = turn(world, "send it again", tool("repeat_step", language="en"), reply(""))
    assert res.sent == ["ask_contact"]


def test_the_ais_near_duplicate_of_a_recent_message_is_dropped(world):
    asked(world)
    turn(world, "pool?", reply("Yes, there is a pool."))
    res, _ = turn(world, "pool??", reply("Yes, there is a pool!"), reply("It's open all day."))
    assert res.sent == ["ai"] and world.wa.texts(GUEST)[-1] == "It's open all day."


def test_the_payment_link_goes_again_when_the_guest_asks_for_it(world):
    book_live(world)
    link = world.wa.texts(GUEST)[-1]
    res, _ = turn(world, "the link is not working, send again", tool("send_payment_link", reason="lost"), reply(""))
    assert res.sent[-1] == "pay" and world.wa.texts(GUEST)[-1] == link


def test_the_rule_drops_only_queued_texts(world):
    asked(world)
    box = bot_outbox(world.bot, GUEST, dry_run=True)
    box.text("ask_contact", ASK_EN)
    box.text("ai", "Yes, there is a pool.")
    gone = turn_module.drop_recent(world.bot, GUEST, box)
    assert [i.key for i in gone] == ["ask_contact"] and [i.key for i in box.queue] == ["ai"]


# --------------------------------------------------------------------------- 5. language switch


def test_english_please_answers_in_english_at_once_and_locks_it(world):
    asked(world, "manglish")
    res, box = turn(world, "English please", tool("set_language", language="en"),
                    tool("repeat_step", language="manglish"), reply(""))  # the lock wins over a later wrong value
    assert texts(box)["ask_contact"] == ASK_EN
    assert world.repo.get_state(GUEST)["agent_lang"]["lock"] == "en"


# --------------------------------------------------------------------------- 6. the fact guards stay


def test_the_fact_guards_and_length_limit_stay():
    src = guard.Sources()
    assert guard.problems("Your room is booked.", src)
    assert guard.problems("The total is ₹9,999.", src)
    assert (guard.MAX_SENTENCES, guard.MAX_WORDS) == (1, 20) and "under 20 words" in guard.TOO_LONG


# --------------------------------------------------------------------------- 7. the prompt


def test_the_prompt_says_owner_texts_go_once_then_the_ai_talks():
    assert ("Owner texts are sent once by the system when a step starts. After that, talk to the guest naturally in "
            "their\n  language: answer what they ask; if a step is still open, remind them briefly in your own words "
            "(e.g. 'Please send\n  your name and mobile number to continue.'), but not when your last message "
            "already asked for it.") in SYSTEM
    assert '("English please", "speak in english", "can u speak\n  english") → set_language' in SYSTEM
    assert "confirm it in the same one sentence as any reminder ('Sure, in English: please send your name and" \
        in SYSTEM
    assert "but not when your last message already asked for it." in SYSTEM
    assert ("A 'yes' / 'ok' right after we said a resort is not\n  available or not ours means they want to book "
            "elsewhere → update_enquiry with no place.") in SYSTEM


# --------------------------------------------------------------------------- eval round (2026-11-25, Haiku)


def test_a_step_said_in_another_language_is_not_sent_again(world):
    """The name ask went in Manglish; after 'Can u speak in english' photos in English never add it in English."""
    asked(world, "manglish")
    res, _ = turn(world, "Can u speak in english", tool("set_language", language="en"),
                  tool("send_photos", kind="default", language="en"), reply(""))
    assert "ask_contact" not in res.sent and ASK_EN not in world.wa.texts(GUEST)


def test_the_prompt_says_nothing_means_an_empty_reply_and_place_is_never_picked():
    assert 'NOTHING means an empty reply, never a line like "Nothing to add".' in SYSTEM
    assert "place = only a place the guest wrote; never pick one for them ('yes', 'ok' name none)." in SYSTEM
    from app.resort.agent.tools import TOOL_SCHEMAS
    place = next(s for s in TOOL_SCHEMAS if s.name == "update_enquiry").parameters["properties"]["place"]
    assert "never one the guest did not name" in place["description"]
