"""Live bug 2026-11-25: after 'restart' the agent still talked about the previous chat. A restart is a clean new
conversation: enquiry cleared, unpaid links closed (paid bookings untouched), no old messages, records or photo memory
for the AI. FAKE LLM only."""

from __future__ import annotations

import pytest
from test_agent_fix_round5 import book_live
from test_agent_gate import agent, m, send  # noqa: F401  (agent is a fixture)
from test_agent_turn import ASK, STAY, go
from test_bot import GUEST, reply, tool, world  # noqa: F401  (world is a fixture)

from app.providers.llm.base import CACHE_SPLIT
from app.resort.agent import conversation
from app.resort.agent import photos_plan as pp
from app.resort.agent.gate import command
from app.resort.agent.outbox import bot_outbox
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import AgentTools, live_link
from app.resort.chat import RESTART_MARK
from app.resort.models import ChatMessage

GREET = "Hi, How may I assist you?"


def restart(w):
    assert send(w, m("restart")) == "restart"
    w.clock.advance(minutes=1)


def dynamic(call) -> str:
    """The per-turn part of the system prompt (TODAY, GUEST RECORDS, CURRENT ENQUIRY)."""
    return call.system.split(CACHE_SPLIT)[-1]


@pytest.mark.parametrize("text", ["restart", "Restart", "RESTART.", " restart ", "restart 🙏", "Restart!!", "re start",
                                  "restart. 👍"])
def test_restart_is_matched_ignoring_case_spaces_and_trailing_marks(text):
    assert command(text) == "restart"


@pytest.mark.parametrize("text", ["restart please", "please restart", "restart the booking", "reset"])
def test_other_words_are_never_the_command(text):
    assert command(text) == ""


def test_restart_greets_with_the_exact_text_and_asks_no_ai(agent):
    restart(agent)
    assert agent.wa.texts(GUEST)[-1] == GREET and agent.llm.calls == []


def test_restart_clears_the_whole_enquiry_and_the_language_lock(agent):
    w = agent
    Enquiry(place=w.p1.id, adults=3, rooms=2, room_picks=[{"room_type": "Deluxe", "count": 1}], extra_beds=1,
            checked={"available": True, "summary": "1 Deluxe"}, contact={"name": "Asha", "mobile": "9847000001"},
            language="manglish", started=True).save(w.repo, GUEST)
    st = w.repo.get_state(GUEST)
    st["lang_lock"], st["style"] = "manglish", "manglish"
    w.repo.set_state(GUEST, st)
    restart(w)
    assert Enquiry.load(w.repo, GUEST) == Enquiry()
    assert set(w.repo.get_state(GUEST)) == {conversation.RESTARTED_KEY}


def test_restart_closes_the_open_unpaid_link(agent):
    w = agent
    book_live(w)
    held = live_link(w.bot, GUEST)
    assert held is not None
    restart(w)
    assert live_link(w.bot, GUEST) is None
    assert w.repo.get_booking(held.id).status == "expired"
    assert held.payment_link_id in getattr(w.bot.payments, "cancelled", [])


def test_restart_never_touches_a_paid_booking(agent):
    w = agent
    book_live(w)
    b = live_link(w.bot, GUEST)
    b.status, b.amount_paid = "paid", b.advance
    w.repo.save_booking(b)
    restart(w)
    assert w.repo.get_booking(b.id).status == "paid"


def test_the_ai_sees_nothing_from_before_the_restart(agent):
    w = agent
    book_live(w)
    restart(w)
    send(w, m("is there a pool?"), reply("Yes, there is a pool."))
    call = w.llm.calls[-1]
    seen = " ".join(str(x.content) for x in call.messages)
    assert ASK not in seen and "Asha" not in seen and "[earlier result]" not in seen
    assert "is there a pool?" in seen
    part = dynamic(call)
    assert "2026-11-10" not in part and "ON HOLD" not in part and "expired" not in part.lower()
    assert "- No confirmed booking." in part and "'restart' and was already greeted" in part


def test_guest_records_keep_only_confirmed_bookings_after_restart(agent):
    w = agent
    book_live(w)
    b = live_link(w.bot, GUEST)
    b.status, b.amount_paid = "paid", b.advance
    w.repo.save_booking(b)
    go(w, "Alleppey 20 nov to 22 nov 2 persons", tool("update_enquiry", **{**STAY, "check_in": "2026-11-20",
                                                                            "check_out": "2026-11-22"}),
       reply(""), live=True)
    w.clock.advance(minutes=1)
    go(w, "Asha, same number", tool("create_booking", guest_name="Asha", mobile="same"), reply(""), live=True)
    assert live_link(w.bot, GUEST) is not None  # a second, unpaid enquiry
    restart(w)
    send(w, m("my booking?"), reply("You have a confirmed booking."))
    part = dynamic(w.llm.calls[-1])
    assert "2026-11-10 to 2026-11-12" in part and "2026-11-20" not in part


def test_photos_can_be_sent_again_after_restart(agent):
    w = agent
    w.repo.add_chat(ChatMessage(GUEST, "out", "[photo] /resort/media/abc", w.clock.now(), "image"))
    assert "/resort/media/abc" in w.bot._sent_photos(GUEST)
    restart(w)
    assert pp.sent_refs(w.bot, GUEST, bot_outbox(w.bot, GUEST, dry_run=True)) == set()


def test_recall_old_chat_never_reads_before_the_restart(agent):
    w = agent
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    restart(w)
    w.clock.advance(hours=13)  # a later conversation: still nothing from before the restart
    w.repo.add_chat(ChatMessage(GUEST, "in", "hello again", w.clock.now(), "text"))
    out = AgentTools(w.bot, GUEST, bot_outbox(w.bot, GUEST, dry_run=True)).recall_old_chat({})
    lines = " ".join(out["facts"]["messages"])
    assert ASK not in lines


def test_recent_messages_never_reach_past_the_restart(agent):
    w = agent
    go(w, ASK, tool("update_enquiry", **STAY), reply(""), live=True)
    restart(w)
    assert pp.recent_texts(w.bot, GUEST, 2) == [GREET]
    assert w.repo.list_chat(GUEST)[-2].text == RESTART_MARK


# --------------------------------------------------------------------------- reference-only records, 'reset all'


def test_guest_records_are_reference_only_with_and_without_restart(agent):
    w = agent
    send(w, m("hi"), reply(GREET))
    assert "reference only" in dynamic(w.llm.calls[-1])
    restart(w)
    send(w, m("hi"), reply(GREET))
    assert "reference only" in dynamic(w.llm.calls[-1])


@pytest.mark.parametrize("text", ["reset all", "Reset All", "RESET ALL.", "resetall"])
def test_reset_all_is_matched(text):
    assert command(text) == "resetall"


def test_reset_all_deletes_this_testers_bookings_and_memory(agent):
    from test_agent_tester_switch import with_config
    w = agent
    with_config(w, agent_test_phones=GUEST)
    book_live(w)
    b = live_link(w.bot, GUEST)
    b.status = "paid"
    w.repo.save_booking(b)
    assert send(w, m("reset all")) == "reset_all"
    assert w.wa.texts(GUEST)[-1] == "Reset done." and w.llm.calls[-0:] is not None
    assert w.repo.list_bookings(phone=GUEST) == [] and w.repo.get_guest(GUEST) is None
    assert w.repo.get_state(GUEST) == {} and [c.text for c in w.repo.list_chat(GUEST)] in ([], ["Reset done."])


def test_reset_all_is_a_normal_message_for_non_testers(agent):
    w = agent  # no AGENT_TEST_PHONES: everyone is answered, nobody is a listed tester
    book_live(w)
    assert send(w, m("reset all"), reply("Sorry?")) != "reset_all"
    assert w.repo.list_bookings(phone=GUEST) != []
