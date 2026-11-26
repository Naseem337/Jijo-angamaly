"""Step AI-4: the guardrails on the AI's OWN text (design section 6), with a FAKE LLM. No real API call anywhere."""

from __future__ import annotations

from test_agent_turn import STAY, go, texts
from test_bot import GUEST, STAFF, answer, reply, tool, world  # noqa: F401  (world is a fixture)

from app.resort.agent import guard
from app.resort.agent.guard import Sources
from app.resort.agent.state import Enquiry
from app.resort.models import Booking


def check_note(w) -> str:
    """The [CHECK] message the AI got for its retry."""
    return w.llm.calls[-1].messages[-1].content


# --------------------------------------------------------------------------- a) figures need a source


# --------------------------------------------------------------------------- b) booked / confirmed only when paid


def test_saying_it_is_not_confirmed_yet_is_fine(world):
    res, box = go(world, "is it booked?", answer("It will be confirmed once the payment is complete."))
    assert not res.retried


def test_confirmed_is_fine_when_a_booking_is_paid(world):
    from datetime import date
    w = world
    b = w.repo.create_hold(Booking(w.p1.id, w.deluxe.id, GUEST, date(2026, 11, 10), date(2026, 11, 12), 2, 0, 1, 8000,
                                   4000, w.clock.now()), w.clock.now())
    b.status = "paid"
    w.repo.save_booking(b)
    res, box = go(w, "is my booking ok?", answer("Yes, your booking is confirmed."))
    assert not res.retried


# --------------------------------------------------------------------------- c) promise words


# --------------------------------------------------------------------------- d) stripped quietly


# --------------------------------------------------------------------------- e) never the same text twice


# --------------------------------------------------------------------------- tool messages are never touched


def test_problems_reads_only_the_ais_text():
    src = Sources(guest="₹100 booked will contact you")  # the guest's own words never trigger anything
    assert guard.problems("Okay.", src) == []
