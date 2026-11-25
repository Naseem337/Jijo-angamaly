"""Step AI-4: the guardrails on the AI's OWN text (design section 6), with a FAKE LLM. No real API call anywhere."""

from __future__ import annotations

from test_agent_turn import STAY, go, texts
from test_bot import GUEST, STAFF, reply, tool, world  # noqa: F401  (world is a fixture)

from app.resort.agent import guard
from app.resort.agent.guard import Sources
from app.resort.agent.state import Enquiry
from app.resort.models import Booking


def check_note(w) -> str:
    """The [CHECK] message the AI got for its retry."""
    return w.llm.calls[-1].messages[-1].content


# --------------------------------------------------------------------------- a) figures need a source


def test_an_unsupported_amount_gets_one_retry_and_the_fixed_reply_goes(world):
    w = world
    res, box = go(w, "is there a pool?", reply("Yes, and a room is ₹5,000 per night."), reply("Yes, there is a pool."))
    assert res.retried and res.outcome == "answered" and res.sent == ["ai"]
    assert texts(box)["ai"] == "Yes, there is a pool."
    note = check_note(w)
    assert note.startswith("[CHECK]") and "₹5,000" in note and "get_price" in note
    assert w.llm.calls[-1].messages[-2].role == "assistant"  # the bad reply, then what was wrong with it


def test_still_wrong_after_the_retry_sends_the_handoff_and_alerts_the_staff(world):
    w = world
    res, box = go(w, "is there a pool?", reply("A room is ₹5,000."), reply("A room is ₹6,000."), live=True)
    assert res.outcome == "handoff" and "ai" not in res.sent and res.sent[:2] == ["handoff", "staff:guard"]
    assert "contact the team directly" in w.wa.texts(GUEST)[-1]
    assert not any("5,000" in x or "6,000" in x for x in w.wa.texts(GUEST))
    alert = w.wa.texts(STAFF)[-1]
    assert "failed its checks twice" in alert and "₹6,000" in alert and len(w.llm.calls) == 2


def test_an_amount_from_get_price_this_turn_is_allowed(world):
    res, box = go(world, "price for Alleppey 10 to 12 nov, 2 persons?", tool("update_enquiry", **STAY), tool("get_price"),
                  reply("The advance is ₹4,000 and the total ₹8,000."))
    assert not res.retried and "price_answer" in res.sent  # a) fine, but g) drops it: the tools already said it
    assert "ai" not in res.sent
    enq = Enquiry.load(world.repo, GUEST)
    src = guard.sources(world.bot, GUEST, enq, res.calls, [], "price?")
    assert guard.problems("The advance is ₹4,000 and the total ₹8,000.", src) == []


def test_an_amount_the_guest_typed_is_allowed(world):
    res, box = go(world, "can you do it for 7000?", reply("I will check if ₹7,000 is possible. One moment"),
                  reply("Rs 7000 is not something I can decide here."))
    assert res.retried and "One moment" in check_note(world) and "7,000" not in check_note(world)
    assert not [g for g in res.guard[1:] if "₹" in g]  # the amount was fine: the guest typed 7000


def test_a_date_or_a_room_count_nobody_gave_is_retried(world):
    res, box = go(world, "rooms?", reply("We have 7 rooms free on 14 Dec."), reply("We have rooms, sir."))
    assert res.retried and len(res.guard) == 2  # (3 rooms would pass: FACTS say the Family Suite has 3 rooms)
    assert "14 Dec" in res.guard[0] and "7 room(s)" in res.guard[1] and texts(box)["ai"] == "We have rooms, sir."


def test_dates_and_rooms_from_the_enquiry_are_allowed(world):
    go(world, "Alleppey 10 nov to 12 nov 2 persons", tool("update_enquiry", **STAY), reply("[no reply]"), live=True)
    world.clock.advance(minutes=1)
    res, box = go(world, "what did I ask for?", reply("Noted: 1 room from 10 Nov to 12 Nov."),  # no tool spoke this turn
                  live=True)
    assert not res.retried and texts(box)["ai"] == "Noted: 1 room from 10 Nov to 12 Nov."
    assert res.sent == ["ai"]  # fix round 5: the step was among the last 2 messages and only the AI spoke: not re-sent


# --------------------------------------------------------------------------- b) booked / confirmed only when paid


def test_confirmed_before_payment_is_retried(world):
    res, box = go(world, "done", reply("Your booking is confirmed!"), reply("Please complete the payment."))
    assert res.retried and "confirmed" in res.guard[0] and texts(box)["ai"] == "Please complete the payment."


def test_saying_it_is_not_confirmed_yet_is_fine(world):
    res, box = go(world, "is it booked?", reply("It will be confirmed once the payment is complete."))
    assert not res.retried


def test_confirmed_is_fine_when_a_booking_is_paid(world):
    from datetime import date
    w = world
    b = w.repo.create_hold(Booking(w.p1.id, w.deluxe.id, GUEST, date(2026, 11, 10), date(2026, 11, 12), 2, 0, 1, 8000,
                                   4000, w.clock.now()), w.clock.now())
    b.status = "paid"
    w.repo.save_booking(b)
    res, box = go(w, "is my booking ok?", reply("Yes, your booking is confirmed."))
    assert not res.retried


# --------------------------------------------------------------------------- c) promise words


def test_promise_words_are_retried(world):
    res, box = go(world, "can I bring my dog?", reply("We will contact you soon about that."),
                  reply("Let me pass this to the owner."))
    assert res.retried and "will contact you" in res.guard[0]


# --------------------------------------------------------------------------- d) stripped quietly


def test_provider_name_booking_id_other_script_and_the_guests_name_are_stripped(world):
    w = world
    w.repo.touch_guest(GUEST, w.clock.now(), name="Anu Mathew")
    res, box = go(w, "how do I pay?", reply("Thank you, Anu, please pay via Razorpay for booking RB-1A2B3C.\nനന്ദി"))
    assert not res.retried and texts(box)["ai"] == "Thank you, please pay for booking."


def test_a_link_not_from_this_turn_is_stripped(world):
    res, box = go(world, "link?", reply("Here it is: https://rzp.io/old123"))
    assert texts(box)["ai"] == "Here it is:"


def test_a_reply_in_another_script_only_is_dropped_and_the_open_step_goes(world):
    res, box = go(world, "hmm", reply("നന്ദി"))
    assert res.outcome == "never_silent" and res.sent == ["say_again_short"]  # nothing open: never silent


def test_clean_keeps_a_link_that_this_turns_tools_gave():
    src = Sources(tools='{"url": "https://rzp.io/new1"}')
    assert guard.clean("Pay here https://rzp.io/new1 via Razorpay.", src) == "Pay here https://rzp.io/new1."


# --------------------------------------------------------------------------- e) never the same text twice


def test_the_ais_line_already_sent_in_this_chat_is_dropped(world):
    w = world
    go(w, "is there a pool?", reply("Yes, there is a pool."), live=True)
    w.clock.advance(minutes=1)
    res, box = go(w, "pool?", reply("Yes, there is a pool."), live=True)
    assert "ai" not in res.sent and res.outcome == "never_silent"
    assert w.wa.texts(GUEST).count("Yes, there is a pool.") == 1


# --------------------------------------------------------------------------- tool messages are never touched


def test_tool_messages_are_never_checked_stripped_or_dropped(world):
    w = world
    go(w, "price Alleppey 10-12 nov 2 persons", tool("update_enquiry", **STAY), tool("get_price"), reply("[no reply]"),
       live=True)
    w.clock.advance(minutes=1)
    res, box = go(w, "price again?", tool("get_price"), reply("[no reply]"), live=True)
    assert res.sent == ["price_answer", "ask_contact"] and not res.retried  # + the open step, which code adds
    prices = [x for x in w.wa.texts(GUEST) if x.startswith("Deluxe: ₹4,000")]
    assert len(prices) == 2 and prices[0] == prices[1]  # the same tool text twice, amounts and all


def test_problems_reads_only_the_ais_text():
    src = Sources(guest="₹100 booked will contact you")  # the guest's own words never trigger anything
    assert guard.problems("Okay.", src) == []
