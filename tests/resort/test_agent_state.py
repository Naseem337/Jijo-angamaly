"""Step AI-1: the Enquiry state and its render, the dry-run outbox and the AGENT_MODE setting."""

from __future__ import annotations

import re
from datetime import date

import pytest

from app.core.client_config import ConfigError
from app.resort.agent.outbox import Outbox
from app.resort.agent.state import NEXT_STEPS, STATE_KEY, Enquiry
from app.resort.config import AGENT_MODES, ResortConfig, load_resort_config
from app.resort.db import ResortRepo

PHONE = "+919847012345"


def full() -> Enquiry:
    return Enquiry(place="kanthaloor", check_in=date(2026, 12, 12), check_out=date(2026, 12, 14), adults=4)


# --------------------------------------------------------------------------- next step and missing


def test_a_new_enquiry_needs_the_location_first():
    e = Enquiry()
    assert e.missing == ["place", "check_in", "check_out", "adults"] and e.next_step == "ask_location"


def test_the_next_step_follows_what_is_known():
    e = Enquiry(place="kanthaloor")
    assert e.next_step == "ask_details" and e.missing == ["check_in", "check_out", "adults"]
    e = full()
    assert e.missing == [] and e.next_step == "check"  # everything known, no room check yet
    e.checked = {"available": True, "summary": "Room 108, Room 109", "rooms": 2}
    assert e.next_step == "ask_contact"
    e.booking_id, e.link_open = "RB-1D9E49", True
    assert e.next_step == "pay"
    e.paid = True
    assert e.next_step == "done"
    assert all(s in NEXT_STEPS for s in ("ask_location", "ask_details", "check", "ask_contact", "pay", "done"))


def test_not_available_asks_for_other_dates():
    e = full()
    e.checked = {"available": False}
    assert e.next_step == "ask_details"
    assert "checked: not available for these dates" in e.render()


# --------------------------------------------------------------------------- render


def test_render_matches_the_design_example():
    e = full()
    e.checked = {"available": True, "summary": "Room 108 Beach View Standard, Room 109 Beach View Standard", "rooms": 2}
    e.booking_id, e.link_open = "RB-1D9E49", True
    assert e.render("Kanthaloor") == (
        "CURRENT ENQUIRY\n"
        "place: Kanthaloor · check-in 12 Dec · check-out 14 Dec (2 nights) · 4 adults · rooms: 2 (code picked)\n"
        "checked: available — Room 108 Beach View Standard, Room 109 Beach View Standard\n"
        "payment link: open, not paid\n"
        "next step: guest should pay the advance")


def test_render_a_blank_enquiry_and_singular_words():
    assert Enquiry().render().splitlines()[1] == "place: not known yet"
    e = Enquiry(place="x", check_in=date(2026, 1, 1), check_out=date(2026, 1, 2), adults=1, children=1, extra_beds=1,
                rooms=1, room_picks=[{"room_type": "cottage", "count": 1}, {"room_number": "103"}])
    line = e.render().splitlines()[1]
    assert "(1 night)" in line and "1 adult + 1 child" in line and "rooms: 1" in line
    assert "picked: 1 x cottage, room 103" in line and "extra beds: 1" in line


def test_render_never_shows_a_booking_id_or_an_amount():
    e = full()
    e.checked = {"available": True, "summary": "Room 108", "rooms": 1, "price": 8000, "total": 8000}
    e.booking_id, e.link_open = "RB-1D9E49", True
    text = e.render("Kanthaloor")
    assert "RB-" not in text and not re.search(r"₹|\d{4,}|\d,\d{3}", text) and "8000" not in text


def test_render_paid():
    e = full()
    e.checked = {"available": True}
    e.booking_id, e.paid = "RB-1D9E49", True
    assert "payment: paid, booking confirmed" in e.render() and e.render().endswith("next step: booking paid and confirmed")


# --------------------------------------------------------------------------- load / save


def test_save_and_load_round_trip(tmp_path):
    repo = ResortRepo(tmp_path / "r.sqlite3")
    e = full()
    e.children, e.rooms, e.extra_beds, e.language = 1, 2, 1, "manglish"
    e.room_picks = [{"room_type": "cottage", "count": 1}]
    e.checked = {"available": True, "summary": "Room 108", "rooms": 2}
    e.booking_id, e.link_open = "RB-1D9E49", True
    e.save(repo, PHONE)
    assert Enquiry.load(repo, PHONE) == e


def test_save_leaves_the_old_bots_state_alone(tmp_path):
    repo = ResortRepo(tmp_path / "r.sqlite3")
    repo.set_state(PHONE, {"lang": "mn", "bk": {"check_in": "2026-12-12"}, "checked": True})
    full().save(repo, PHONE)
    state = repo.get_state(PHONE)
    assert state["lang"] == "mn" and state["bk"] == {"check_in": "2026-12-12"} and state["checked"] is True
    assert STATE_KEY in state
    state.pop(STATE_KEY)  # the old bot saving its own state never needs to know about the agent's
    repo.set_state(PHONE, state)
    assert Enquiry.load(repo, PHONE) == Enquiry()


def test_load_survives_missing_or_broken_data(tmp_path):
    repo = ResortRepo(tmp_path / "r.sqlite3")
    assert Enquiry.load(repo, PHONE) == Enquiry()
    repo.set_state(PHONE, {STATE_KEY: {"check_in": "not-a-date", "language": "klingon", "extra_beds": None}})
    e = Enquiry.load(repo, PHONE)
    assert e.check_in is None and e.language == "en" and e.extra_beds == 0


# --------------------------------------------------------------------------- the outbox


def test_the_outbox_sends_in_order():
    sent: list = []
    box = Outbox(PHONE, lambda to, text: sent.append(("text", to, text)), lambda to, refs: sent.append(("photos", to, refs)))
    box.text("rooms_ok", "Yes, rooms are available for your dates.")
    box.photos("photos:2", ["a.jpg", "b.jpg"])
    box.text("ask_contact", "Please share your name and mobile number to proceed.")
    assert box.keys == ["rooms_ok", "photos:2", "ask_contact"] and sent == []  # queued, nothing sent yet
    box.flush()
    assert sent == [("text", PHONE, "Yes, rooms are available for your dates."), ("photos", PHONE, ["a.jpg", "b.jpg"]),
                    ("text", PHONE, "Please share your name and mobile number to proceed.")]
    assert box.queue == [] and box.keys == ["rooms_ok", "photos:2", "ask_contact"]


def test_a_dry_run_outbox_records_but_never_sends():
    sent: list = []
    box = Outbox(PHONE, lambda to, text: sent.append(text), lambda to, refs: sent.append(refs), dry_run=True)
    box.text("greet", "Hi, How may I assist you?")
    box.photos("photos:1", ["a.jpg"])
    done = box.flush()
    assert sent == [] and [i.key for i in done] == ["greet", "photos:1"]
    assert box.sent[0].text == "Hi, How may I assist you?" and box.sent[1].refs == ("a.jpg",)


def test_an_outbox_with_no_senders_does_not_crash():
    box = Outbox(PHONE)
    box.text("x", "hello")
    assert [i.key for i in box.flush()] == ["x"]


# --------------------------------------------------------------------------- AGENT_MODE


def test_agent_mode_defaults_to_old(monkeypatch):
    monkeypatch.delenv("AGENT_MODE", raising=False)
    assert load_resort_config().agent_mode == "old"


@pytest.mark.parametrize("mode", AGENT_MODES)
def test_agent_mode_comes_from_the_environment(monkeypatch, mode):
    monkeypatch.setenv("AGENT_MODE", mode.upper())
    assert load_resort_config().agent_mode == mode


def test_a_bad_agent_mode_is_refused(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "robot")
    with pytest.raises(ConfigError):
        load_resort_config()
    assert ResortConfig.model_fields["agent_mode"].default == "old"


def test_a_marker_shows_in_sent_but_sends_nothing():
    sent: list = []
    box = Outbox(PHONE, lambda to, text: sent.append(text), lambda to, refs: sent.append(refs))
    box.text("rooms_ok", "Yes.")
    box.marker("photos:pending")
    box.flush()
    assert sent == ["Yes."] and box.keys == ["rooms_ok", "photos:pending"]
