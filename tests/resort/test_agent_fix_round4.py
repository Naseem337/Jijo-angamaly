"""Agent fix round 4 (room choice, photos at any stage, recent past dates, eval checks), with the FAKE LLM."""

from __future__ import annotations

from datetime import date

import pytest
import yaml
from test_agent_fix_round1 import late_month
from test_agent_fix_round3 import ROOT, call, eval_module, numbered, spec
from test_agent_photos import block, keys, pw  # noqa: F401  (pw is a fixture)
from test_agent_photos import run as prun
from test_agent_tools_2c import book
from test_agent_turn import STAY
from test_bot import GUEST, world  # noqa: F401  (world is a fixture)

from app.resort.agent.prompt import SYSTEM
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import RECENT_PAST_DAYS, build_check_in
from app.resort.models import RoomType

PHOTO_RULE = "Never refuse, never ask for dates first."


# --------------------------------------------------------------------------- 1c) the guest CHOOSES a room


def test_choosing_a_room_sends_room_picked_and_the_new_rooms_sentence(world):
    call(world, "update_enquiry", STAY, said="Alleppey 10 to 12 nov 2 adults")
    result, sent, _ = call(world, "update_enquiry", {**STAY, "room_picks": [{"room_type": "Family Suite"}]},
                           said="family suite venam")
    assert result["sent"][:2] == ["room_picked", "rooms_plain"] and result["sent"][-1] == "ask_contact"
    assert sent["room_picked"] == "Sure, Family Suite is free for your dates."
    assert result["facts"]["rooms_offered"] == "1 Family Suite"


def test_choosing_a_room_by_its_number_picks_its_room_type(world):
    numbered(world, world.family, ["201", "202"])
    call(world, "update_enquiry", STAY, said="Alleppey 10 to 12 nov 2 adults")
    result, _, _ = call(world, "update_enquiry", {**STAY, "room_picks": [{"room_number": "202"}]}, said="room 202")
    assert "room_picked" in result["sent"] and result["facts"]["rooms_offered"] == "1 Family Suite"


def test_choosing_a_room_after_the_link_closes_it_then_new_summary_and_link(world):
    book(world, live=True)
    old = world.repo.list_bookings(phone=GUEST, status="hold", limit=1)[0]
    world.clock.advance(minutes=1)
    result, sent, _ = call(world, "update_enquiry", {**STAY, "room_picks": [{"room_type": "Family Suite"}]},
                           said="change to family suite", live=True)
    assert result["sent"] == ["room_picked", "rooms_plain", "summary", "pay"]
    assert world.repo.get_booking(old.id).status != "hold"  # the old link is closed
    new = world.repo.list_bookings(phone=GUEST, status="hold", limit=1)[0]
    assert new.id != old.id and new.room_type_id == world.family.id


def test_the_code_picking_by_priority_never_sends_room_picked(world):
    result, _, _ = call(world, "update_enquiry", STAY, said="Alleppey 10 to 12 nov 2 adults")
    assert "room_picked" not in result["sent"]


def test_the_prompt_and_room_picks_say_what_choosing_is():
    assert "Guest CHOOSES a room (this one, take it, change to X, 'cottage venam', 'room 103', a swipe-reply" in SYSTEM
    desc = spec("update_enquiry").parameters["properties"]["room_picks"]["description"]
    assert "'cottage venam', 'room 103'" in desc and "send_photos, not room_picks" in desc


# --------------------------------------------------------------------------- 1d) photos at any stage


def test_the_photo_rule_is_in_the_prompt_and_the_tool():
    assert "Guest asks to see rooms or the resort in any words (pics?, how does it look, photo undo?) → call send_photos" \
        in SYSTEM and PHOTO_RULE in SYSTEM
    assert PHOTO_RULE in spec("send_photos").description and "after payment" in spec("send_photos").description


def test_photos_before_any_dates_go_without_asking_for_dates(pw):  # noqa: F811
    w = pw
    Enquiry(place=w.p1.id).save(w.repo, GUEST)
    result, items, _ = prun(w, "send_photos", {"kind": "default"})
    assert keys(items)[:2] == ["room_label", "photos:2"] and "form" not in keys(items)[:2]


def test_more_photos_go_on_with_the_next_free_room_number(pw):  # noqa: F811
    w = pw
    rt = w.repo.get_room_type(w.deluxe.id)
    own = {"108": [w.refs["deluxe"][0]], "109": [w.refs["deluxe"][1]]}
    w.repo.save_room_type(RoomType(**{**rt.__dict__, "room_list": ["108", "109"], "number_photos": own,
                                      "photo_urls": []}))
    prun(w, "update_enquiry", STAY)  # Room 108 is shown
    prun(w, "send_photos", {"kind": "more"})  # the Family Suite (another free room type)
    result, items, _ = prun(w, "send_photos", {"kind": "more"})
    assert keys(items)[:2] == ["room_label", "photos:1"] and items[0][1] == "*Room 109 - Deluxe*"


def test_after_payment_the_own_booking_never_makes_the_room_not_free(pw):  # noqa: F811
    w = pw
    prun(w, "update_enquiry", STAY)
    prun(w, "create_booking", {"guest_name": "Asha", "mobile": "same"})
    held = w.repo.list_bookings(phone=GUEST, status="hold", limit=1)[0]
    w.bot.mark_paid(booking_id=held.id, payment_id="pay_1")
    block(w, w.deluxe, 1)  # the other Deluxe is taken by someone else: on these dates every Deluxe is booked
    result, items, _ = prun(w, "send_photos", {"kind": "room_type", "room_type": "Deluxe"})
    assert "room_not_free" not in keys(items) and "no_other_room" not in keys(items)


# --------------------------------------------------------------------------- 2) recent past dates (owner rule)


@pytest.mark.parametrize("today,parts,want,told", [
    (date(2026, 11, 25), (20, 11, None), date(2026, 11, 20), True),  # 5 days ago: past_date
    (date(2026, 11, 25), (26, 10, None), date(2026, 10, 26), True),  # 30 days ago: still past_date
    (date(2026, 11, 25), (25, 10, None), date(2027, 10, 25), False),  # 31 days ago: next year
    (date(2026, 11, 25), (10, 1, None), date(2027, 1, 10), False),  # the example: 10 Jan -> 10 Jan 2027
    (date(2026, 11, 25), (25, 11, None), date(2026, 11, 25), False),  # today is not past
    (date(2027, 1, 5), (28, 12, None), date(2026, 12, 28), True),  # over the new year: 8 days ago
    (date(2026, 11, 25), (20, None, None), date(2026, 12, 20), False),  # a day alone: next time it comes
])
def test_a_date_without_a_year_that_passed_days_ago_is_past(today, parts, want, told):
    assert build_check_in(parts, today) == (want, told)
    assert RECENT_PAST_DAYS == 30


def test_a_recent_past_date_sends_past_date_with_the_owners_words(world):
    late_month(world)  # 2026-11-25
    result, sent, _ = call(world, "update_enquiry", {"place": "Alleppey", "check_in": {"day": 20, "month": 11},
                                                     "check_out": {"day": 22, "month": 11}, "adults": 2},
                           said="Alleppey 20 Nov to 22 Nov, 2 adults")
    assert result["sent"] == ["past_date"]
    assert sent["past_date"] == "20 Nov has already passed. Please send your check-in and check-out dates again."
    assert Enquiry.load(world.repo, GUEST).check_in is None


def test_a_date_long_past_moves_to_next_year(world):
    late_month(world)
    result, _, _ = call(world, "update_enquiry", {"place": "Alleppey", "check_in": {"day": 10, "month": 1}, "nights": 2,
                                                  "adults": 2}, said="Alleppey 10 Jan, 2 nights")
    assert result["facts"]["check_in"] == "2027-01-10"


# --------------------------------------------------------------------------- 3) the eval's new checks and scenarios


def test_the_new_eval_checks(monkeypatch):
    ev = eval_module(monkeypatch)
    keys_ = ["rooms_ok", "rooms_plain", "glimpse", "photos:3", "room_label", "photos:1", "room_label", "photos:1",
             "ask_contact"]
    order = {"in_order": ["rooms_ok", "rooms_plain|rooms_note", "glimpse", "room_label", "photos", "room_label",
                          "photos", "ask_contact"]}
    assert ev.check_turn(order, "answered", [], keys_, []) == []
    assert ev.check_turn(order, "answered", [], ["ask_contact", "rooms_ok"], [])
    assert ev.check_turn({"max_words": 3}, "answered", [], [], [], "one two three four")
    assert ev.check_turn({"max_words": 3}, "answered", [], [], [], "") == []
    assert ev.check_turn({"ai_empty": True}, "answered", [], [], [], "Done.")
    assert ev.check_turn({"ai_required": True}, "answered", [], [], [], "")
    assert ev.check_turn({"only_sent": ["handoff"]}, "answered", [], ["handoff", "staff:alert"], []) == []
    assert ev.check_turn({"only_sent": ["handoff"]}, "answered", [], ["ai", "handoff"], [])
    assert ev.check_turn({"no_duplicates": True}, "answered", [], [], ["Yes, secure.", "yes secure"])


def test_a_scenario_can_switch_a_resort_off(monkeypatch):
    ev = eval_module(monkeypatch)
    sc = {"id": "x", "closed": ["Kanthalloor"], "turns": [{"guest": "[wait 1h]"}]}
    seen = {}
    real = ev.scratch_world

    def spy(llm):
        w = real(llm)
        seen["w"] = w
        return w

    monkeypatch.setattr(ev, "scratch_world", spy)
    monkeypatch.setattr(ev.ph, "_shrink", lambda data: None)
    ev.run_scenario(sc, None)
    props = {p.name: p.active for p in seen["w"].repo.list_properties(active_only=False)}
    assert props["Vibes Kanthalloor"] is False and props["Pozhiyoram Resort"] is True


def test_the_round_4_scenarios_are_there():
    ids = {s["id"] for s in yaml.safe_load((ROOT / "tests/eval/agent_scenarios.yaml").read_text(encoding="utf-8"))}
    assert {"closed_place_then_yes", "not_our_place", "not_our_place_2", "short_answer", "rooms_check_labels",
            "one_room_for_four", "ok_after_contact_ask", "photos_after_link", "other_room_then_choose",
            "casual_photos", "no_expiry_talk", "paid_not_paid", "is_it_secure", "pets_no_policy",
            "recent_past_date"} <= ids
