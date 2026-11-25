"""Step AI-4: the gate (everything before the AI), with a FAKE LLM. No real API call anywhere."""

from __future__ import annotations

import itertools
from datetime import datetime

import pytest
from test_bot import GUEST, STAFF, reply, tool, world  # noqa: F401  (world is a fixture)

from app.conversation.voice_in import VoiceResult
from app.resort.agent import gate
from app.resort.agent.gate import IMAGE_NOTE, command
from app.resort.agent.state import Enquiry
from app.resort.chat import RESTART_MARK
from app.resort.i18n import t
from app.resort.models import ChatMessage
from app.whatsapp.parse import InboundMessage

STAY = {"place": "Alleppey", "check_in": "2026-11-10", "check_out": "2026-11-12", "adults": 2}
_ids = itertools.count(1)


@pytest.fixture()
def agent(world):  # noqa: F811
    world.bot.config = world.bot.config.model_copy(update={"agent_mode": "agent"})
    return world


def m(text="", kind="text", **kw) -> InboundMessage:
    return InboundMessage(kw.pop("id", f"wamid.g{next(_ids)}"), GUEST, kind, datetime(2026, 11, 2, 4, 30), text=text, **kw)


def send(w, message, *script):
    w.llm.script.extend(script)
    return w.bot.handle(message)


def last_user(w) -> str:
    return w.llm.calls[-1].messages[-1].content


# --------------------------------------------------------------------------- AGENT_MODE


def test_agent_mode_old_is_the_default_and_never_reaches_the_gate(world, monkeypatch):  # noqa: F811
    assert world.bot.config.agent_mode == "old"
    monkeypatch.setattr(gate, "handle", lambda *a, **k: pytest.fail("the gate ran in old mode"))
    world.bot.handle(m("hi"))
    assert world.wa.texts()[-1] == "Hi, How may I assist you?"  # the old bot answered


def test_agent_mode_agent_routes_every_message_to_the_gate(agent, monkeypatch):
    seen = []
    monkeypatch.setattr(gate, "handle", lambda bot, msg: seen.append(msg.text) or "gate")
    assert agent.bot.handle(m("hello")) == "gate" and seen == ["hello"]


def test_in_agent_mode_the_ai_answers(agent):
    assert send(agent, m("is there a pool?"), reply("Yes, there is a pool.")) == "answered"
    assert agent.wa.texts() == ["Yes, there is a pool."] and len(agent.llm.calls) == 1


# --------------------------------------------------------------------------- dedupe


def test_the_same_message_id_is_answered_once(agent):
    first = m("is there a pool?", id="wamid.same")
    assert send(agent, first, reply("Yes, there is a pool.")) == "answered"
    assert agent.bot.handle(m("is there a pool?", id="wamid.same")) == "duplicate"
    assert len(agent.llm.calls) == 1 and len(agent.wa.texts()) == 1


# --------------------------------------------------------------------------- STOP / START / restart


@pytest.mark.parametrize("text, want", [("STOP", "stop"), ("stop.", "stop"), (" Start! ", "start"), ("RESTART", "restart"),
                                        ("stop please", ""), ("please stop", ""), ("start booking", ""), ("reset", ""),
                                        ("restart the booking", "")])
def test_only_the_exact_command_words_are_commands(text, want):
    assert command(text) == want


def test_stop_and_start_are_answered_by_code_with_no_ai(agent):
    assert send(agent, m("STOP")) == "opt_out"
    assert agent.repo.get_state(GUEST)["opted_out"] is True and agent.repo.get_guest(GUEST).offers_consent is False
    assert agent.wa.texts()[-1] == t("opt_out", "en")
    assert send(agent, m("start")) == "opt_in"
    assert agent.repo.get_state(GUEST)["opted_out"] is False and agent.repo.get_guest(GUEST).offers_consent is True
    assert agent.wa.texts()[-1] == t("opt_in", "en") and agent.llm.calls == []


def test_start_when_not_stopped_and_stop_in_a_sentence_go_to_the_ai(agent):
    assert send(agent, m("START"), reply("Sure, how may I help?")) == "answered"
    assert send(agent, m("stop sending photos"), reply("Okay.")) == "answered"
    assert len(agent.llm.calls) == 2 and not agent.repo.get_state(GUEST).get("opted_out")


def test_restart_clears_the_enquiry_greets_and_keeps_stop(agent):
    Enquiry(place=agent.p1.id, adults=2).save(agent.repo, GUEST)
    st = agent.repo.get_state(GUEST)
    st["opted_out"] = True
    agent.repo.set_state(GUEST, st)
    assert send(agent, m("Restart")) == "restart"
    st = agent.repo.get_state(GUEST)
    assert Enquiry.load(agent.repo, GUEST).place is None and st["opted_out"] is True
    assert set(st) == {"opted_out", "restarted_at"}  # live bug 2026-11-25: the restart time is kept
    assert agent.wa.texts()[-1] == "Hi, How may I assist you?" and agent.llm.calls == []
    assert agent.repo.list_chat(GUEST)[-2].text == RESTART_MARK  # the greeting belongs to the new conversation


# --------------------------------------------------------------------------- staff pause


def test_a_paused_chat_gets_no_reply_and_resumes_after_24_hours(agent):
    agent.repo.set_paused(GUEST, True, agent.clock.now())
    assert send(agent, m("hello?")) == "paused"
    assert agent.wa.texts() == [] and agent.llm.calls == []
    assert agent.repo.list_chat(GUEST)[-1].text == "hello?"  # still saved for the staff
    agent.clock.advance(hours=25)
    assert send(agent, m("anyone?"), reply("Yes, how may I help?")) == "answered"
    assert agent.repo.is_paused(GUEST)[0] is False and agent.wa.texts() == ["Yes, how may I help?"]


# --------------------------------------------------------------------------- voice


class Voice:
    def __init__(self, *results):
        self.results, self.calls = list(results), 0

    def process(self, media_id):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_a_voice_note_becomes_text_for_the_ai(agent):
    voice = Voice(VoiceResult(text="is there a pool"))
    agent.bot._voice_in = lambda: voice
    assert send(agent, m(kind="audio", media_id="a1"), reply("Yes, there is a pool.")) == "answered"
    assert last_user(agent).endswith("is there a pool") and voice.calls == 1
    row = [r for r in agent.repo.list_chat(GUEST) if r.direction == "in"][-1]
    assert row.text == "is there a pool" and row.source == "voice"


def test_a_voice_hiccup_is_retried_once(agent):
    voice = Voice(RuntimeError("download timed out"), VoiceResult(text="price please"))
    agent.bot._voice_in = lambda: voice
    assert send(agent, m(kind="audio", media_id="a1"), reply("Sure.")) == "answered" and voice.calls == 2


def test_a_failed_voice_note_gets_voice_problem_and_no_ai(agent):
    voice = Voice(VoiceResult(problem="failed", detail="timeout"), VoiceResult(problem="failed", detail="timeout"))
    agent.bot._voice_in = lambda: voice
    assert send(agent, m(kind="audio", media_id="a1")) == "voice_problem"
    assert agent.wa.texts() == [t("voice_problem", "en")] and voice.calls == 2 and agent.llm.calls == []
    assert "timeout" in agent.repo.list_chat(GUEST)[0].text


def test_an_unclear_voice_note_is_not_retried(agent):
    voice = Voice(VoiceResult(problem="unclear"))
    agent.bot._voice_in = lambda: voice
    assert send(agent, m(kind="audio", media_id="a1")) == "voice_problem" and voice.calls == 1


# --------------------------------------------------------------------------- image, sticker, unsupported


def test_an_image_reaches_the_ai_as_a_note_and_it_may_check_the_payment(agent):
    out = send(agent, m(kind="image", media_id="img1"), tool("check_payment"), reply("I could not see a payment yet."))
    assert out == "answered" and IMAGE_NOTE in agent.llm.calls[0].messages[-1].content
    assert agent.llm.calls[1].messages[-2].tool_calls[0].name == "check_payment"
    assert agent.repo.list_chat(GUEST)[0].text == "[image]"


def test_an_image_with_a_caption_keeps_the_caption(agent):
    send(agent, m("payment done", kind="image", media_id="img1"), reply("Let me see."))
    assert last_user(agent).endswith(IMAGE_NOTE + ": payment done")


def test_an_image_never_confirms_a_booking(agent):
    out = send(agent, m(kind="image", media_id="img1"), reply("Your booking is confirmed!"),
               reply("I could not see a payment yet."))
    assert out == "answered" and agent.wa.texts() == ["I could not see a payment yet."]


@pytest.mark.parametrize("kind", ["reaction", "sticker"])
def test_reactions_and_stickers_get_no_reply(agent, kind):
    assert send(agent, m(kind=kind)) == "silent"
    assert agent.wa.texts() == [] and agent.llm.calls == []


@pytest.mark.parametrize("kind", ["location", "contacts", "video"])
def test_location_contact_and_video_get_the_unsupported_line(agent, kind):
    assert send(agent, m(kind=kind)) == "unsupported"
    assert agent.wa.texts() == [t("unsupported", "en")] and agent.llm.calls == []


# --------------------------------------------------------------------------- swipe-reply


def test_a_swipe_reply_puts_the_note_line_before_the_guest_message(agent):
    agent.repo.add_chat(ChatMessage(GUEST, "out", "Do you need an extra bed?", agent.clock.now(), "text", "wamid.q1"))
    send(agent, m("yes", context_id="wamid.q1"), reply("Okay."))
    assert last_user(agent).splitlines()[-2:] == ['[Guest swiped-replied to your message: "Do you need an extra bed?"]',
                                                 "yes"]


# --------------------------------------------------------------------------- a full turn


def test_a_full_turn_gate_ai_tools_guard_outbox_in_order(agent):
    out = send(agent, m("Alleppey 10 nov to 12 nov 2 persons, is there a pool?"), tool("update_enquiry", **STAY),
               tool("get_price"), reply("Yes, there is a pool."))
    assert out == "answered"
    texts = agent.wa.texts()
    assert texts[0] == "Yes, there is a pool."  # the AI's line first
    assert texts[1:4] == ["Yes, rooms are available for your dates.", "For 2 persons: 1 room(s).", "*Deluxe*"]
    assert texts[4].startswith("Deluxe: ₹4,000 per room per night.")  # the tool messages in queue order
    assert texts[5] == "Please share your name and mobile number to proceed."  # the open step always goes last
    assert agent.wa.texts(STAFF) == []
    stored = [r.text for r in agent.repo.list_chat(GUEST) if r.direction == "out"]
    assert stored == texts


def test_the_gate_with_a_dry_run_sends_nothing(agent):
    class Spy:
        calls: list = []

        def __getattr__(self, name):
            return lambda *a, **k: Spy.calls.append(name) or "wamid.spy"

    agent.bot.wa = Spy()
    agent.llm.script.append(reply("Yes, there is a pool."))
    assert gate.handle(agent.bot, m("is there a pool?"), dry_run=True) == "answered"
    assert [c for c in Spy.calls if c != "mark_read"] == []
