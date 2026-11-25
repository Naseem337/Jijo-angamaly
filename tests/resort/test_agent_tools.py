"""Step AI-2a: update_enquiry and get_price, called directly (no LLM): the owner's texts through the outbox, the result
shape, the room rules and a change after the payment link. Rates in the test world: Deluxe 4000, Family Suite 8000."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from test_bot import GUEST, hold, world  # noqa: F401  (world is a fixture)

from app.resort.agent import photos_plan as photos_module
from app.resort.agent import tools as tools_module
from app.resort.agent.outbox import Outbox
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import TOOLS, AgentTools
from app.resort.i18n import t
from app.resort.models import Booking

FORM = "Please share these details:\n\nCheck in :\nCheck out :\nNumber of adults (above 6 years) :\nNumber of rooms:"
STAY = {"place": "Alleppey", "check_in": "2026-11-10", "check_out": "2026-11-12"}  # today in the tests is 2 Nov 2026


def agent(w, phone=GUEST):
    box = Outbox(phone, dry_run=True)
    return AgentTools(w.bot, phone, box), box


def update(w, args, phone=GUEST):
    tools, box = agent(w, phone)
    result = tools.update_enquiry(args)
    return result, {i.key: i.text for i in box.queue}, box


def block(w, rt, rooms, check_in=date(2026, 11, 10), check_out=date(2026, 11, 12)):
    """Another guest holds `rooms` of this room type."""
    b = Booking(w.p1.id, rt.id, "+919811111111", check_in, check_out, 2 * rooms, 0, rooms, 8000, 4000, w.clock.now(),
                hold_expires_at=w.clock.now() + timedelta(minutes=15))
    w.repo.create_hold(b, w.clock.now())


def assert_shape(result):
    assert set(result) == {"ok", "facts", "sent", "next_step", "note"}
    assert isinstance(result["ok"], bool) and isinstance(result["facts"], dict) and isinstance(result["sent"], list)
    assert isinstance(result["next_step"], str) and isinstance(result["note"], str)


# --------------------------------------------------------------------------- the rules of the file


def test_the_agent_files_never_read_the_guests_words():
    for module in (tools_module, photos_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "import re" not in source and "re.compile" not in source and "re.search" not in source
        assert "re.match" not in source and "re.sub" not in source and "re.findall" not in source
    assert {x.name for x in TOOLS} == {"update_enquiry", "get_price", "send_photos", "send_map", "create_booking",
                                       "check_payment", "send_payment_link", "my_bookings", "change_name",
                                       "talk_to_owner", "recall_old_chat"}


# --------------------------------------------------------------------------- 1. place missing


def test_place_missing_sends_the_owners_location_question(world):
    w = world
    result, texts, _ = update(w, {})
    assert_shape(result)
    assert result["ok"] and result["sent"] == ["ask_location"] and result["next_step"] == "ask_location"
    assert texts["ask_location"] == "Sure, Kindly confirm the Location 📍\nAlleppey or Munnar ?"


def test_an_unknown_place_asks_the_location_too(world):
    result, texts, _ = update(world, {"place": "Goa", "check_in": "2026-11-10"})
    assert result["sent"] == ["ask_location"] and "ask_location" in texts


def test_a_switched_off_resort_gets_closed_place_and_is_not_saved(world):
    w = world
    w.p2.active = False
    w.repo.save_property(w.p2)
    result, texts, _ = update(w, {"place": "Kanthalloor", "check_in": "2026-11-10", "adults": 2})
    assert result["sent"] == ["closed_place"] and texts["closed_place"] == "Sorry, Munnar is currently not available."
    assert Enquiry.load(w.repo, GUEST).place is None and result["next_step"] == "ask_location"


def test_the_language_picks_the_matching_text(world):
    result, texts, _ = update(world, {"place": "Alleppey", "check_in": "2026-11-10", "check_out": "2026-11-12", "adults": 2,
                                      "language": "manglish"})
    assert texts["rooms_ok"] == t("rooms_ok", "mn") and texts["rooms_ok"] != t("rooms_ok", "en")
    assert Enquiry.load(world.repo, GUEST).language == "manglish"


# --------------------------------------------------------------------------- 2. dates or persons missing


def test_nothing_but_the_place_sends_the_full_form(world):
    result, texts, _ = update(world, {"place": "Alleppey"})
    assert_shape(result)
    assert result["sent"] == ["form"] and texts["form"] == FORM and result["next_step"] == "ask_details"
    assert Enquiry.load(world.repo, GUEST).place == world.p1.id


def test_the_form_has_only_the_missing_lines(world):
    result, texts, _ = update(world, {"place": "Alleppey", "check_in": "2026-11-10", "adults": 2})
    assert texts["form"] == "Please share these details:\n\nCheck out :\nNumber of rooms:"
    assert result["facts"]["missing"] == ["check_out"]
    # the saved persons and check-in are kept: the next call only adds the check-out
    result, texts, _ = update(world, {"place": "Alleppey", "check_out": "2026-11-12"})
    assert result["facts"]["available"] and "rooms_ok" in texts


def test_persons_missing_asks_only_for_persons(world):
    _, texts, _ = update(world, {**STAY})
    assert texts["form"] == "Please share these details:\n\nNumber of adults (above 6 years) :\nNumber of rooms:"


def test_nights_give_the_check_out(world):
    result, _, _ = update(world, {"place": "Alleppey", "check_in": "2026-11-10", "nights": 2, "adults": 2})
    assert result["facts"]["check_out"] == "2026-11-12" and result["facts"]["nights"] == 2


# --------------------------------------------------------------------------- 3. past date


def test_a_date_that_just_passed_is_told_and_not_saved(world):
    result, texts, _ = update(world, {"place": "Alleppey", "check_in": "2026-10-30", "check_out": "2026-11-01", "adults": 2,
                                   "month_given": True})
    assert result["sent"] == ["past_date"]
    assert texts["past_date"] == "30 Oct has already passed. Please send your check-in and check-out dates again."
    enq = Enquiry.load(world.repo, GUEST)
    assert enq.check_in is None and enq.check_out is None and result["next_step"] == "ask_details"


def test_a_date_months_ago_means_the_next_time_it_comes(world):
    result, _, _ = update(world, {"place": "Alleppey", "check_in": "2025-12-25", "nights": 2, "adults": 2, "month_given": True})
    assert (result["facts"]["check_in"], result["facts"]["check_out"]) == ("2026-12-25", "2026-12-27")


def test_a_check_out_before_the_check_in_is_a_problem_not_a_message(world):
    result, texts, _ = update(world, {"place": "Alleppey", "check_in": "2026-11-12", "check_out": "2026-11-10", "adults": 2})
    assert not result["ok"] and "after check-in" in result["facts"]["problem"] and texts == {}


# --------------------------------------------------------------------------- 4. available


def test_available_sends_the_owners_messages_in_order_with_the_price_in_facts(world):
    w = world
    result, texts, box = update(w, {**STAY, "adults": 2})
    assert_shape(result)
    assert result["sent"] == ["rooms_ok", "rooms_plain", "ask_contact"]  # this resort has no glimpse or photos yet
    assert [i.key for i in box.queue] == result["sent"]
    assert texts["rooms_ok"] == "Yes, rooms are available for your dates."
    assert texts["rooms_plain"] == "For 2 persons: 1 room(s)."
    assert texts["ask_contact"] == "Please share your name and mobile number to proceed."
    assert result["next_step"] == "ask_contact"
    facts = result["facts"]
    assert facts["available"] and facts["rooms"] == 1 and facts["rooms_offered"] == "1 Deluxe"
    assert facts["price"] == {"total": 8000, "advance_now": 4000, "balance_at_property": 4000, "per_room_per_night": 4000}
    enq = Enquiry.load(w.repo, GUEST)
    assert enq.checked["available"] and enq.adults == 2 and enq.next_step == "ask_contact"
    assert enq.rooms is None  # the guest never said a number of rooms


def test_the_same_stay_again_sends_nothing(world):
    update(world, {**STAY, "adults": 2})
    result, texts, _ = update(world, {**STAY, "adults": 2})
    assert result["sent"] == [] and texts == {} and "unchanged" in result["note"]


def test_only_the_persons_changed_gives_the_new_rooms_sentence_and_the_name_ask(world):
    update(world, {**STAY, "adults": 2})
    result, texts, _ = update(world, {**STAY, "adults": 3})  # 3 adults: 2 rooms by default (2 per room)
    assert result["sent"] == ["rooms_note", "ask_contact"] and "rooms_ok" not in texts  # same dates: not said twice
    assert result["facts"]["rooms"] == 2 and result["facts"]["extra_bed_question"] is False


def test_new_dates_say_rooms_available_again(world):
    update(world, {**STAY, "adults": 2})
    result, _, _ = update(world, {**STAY, "check_in": "2026-11-20", "check_out": "2026-11-22", "adults": 2})
    assert result["sent"][0] == "rooms_ok"


def test_a_field_the_ai_leaves_out_is_never_lost(world):
    update(world, {**STAY, "adults": 2})
    result, _, _ = update(world, {"place": "Alleppey", "rooms": 2})
    assert result["facts"]["adults"] == 2 and result["facts"]["rooms"] == 2
    assert result["facts"]["check_in"] == "2026-11-10"


# --------------------------------------------------------------------------- rooms given versus the fewest rooms


def test_no_number_of_rooms_means_the_fewest_that_fit(world):
    result, texts, _ = update(world, {**STAY, "adults": 4})  # 2 adults per room
    assert result["facts"]["rooms"] == 2 and result["facts"]["rooms_offered"] == "2 Deluxe"
    assert texts["rooms_plain"] == "For 4 persons: 2 room(s)." and "rooms_note" not in texts
    three, texts, _ = update(world, {**STAY, "adults": 3})  # 3 adults: 2 rooms, never an extra-bed question
    assert three["facts"]["rooms"] == 2 and three["facts"]["extra_bed_question"] is False
    assert texts["rooms_note"] == ("For 3 persons, 2 rooms are needed (2 per room). Or 3 persons can stay in 1 room(s), "
                                   "up to 3 per room.") and "ask_extra_bed" not in texts


def test_a_number_of_rooms_the_guest_gave_is_used(world):
    result, texts, _ = update(world, {**STAY, "adults": 3, "rooms": 1})  # fewer rooms: 3 adults in one room
    assert result["facts"]["rooms"] == 1 and result["facts"]["rooms_offered"] == "1 Deluxe"
    assert texts["rooms_plain"] == "For 3 persons: 1 room(s)."
    more, _, _ = update(world, {"place": "Alleppey", "adults": 2, "rooms": 2})
    assert more["facts"]["rooms"] == 2 and more["facts"]["rooms_offered"] == "2 Deluxe"
    assert Enquiry.load(world.repo, GUEST).rooms == 2


def test_too_few_rooms_say_the_minimum_and_the_check_goes_on(world):
    result, texts, _ = update(world, {**STAY, "adults": 9, "rooms": 1})
    assert result["sent"][0] == "min_rooms" and texts["min_rooms"] == "Max 3 persons per room, so 9 persons need at least 3 rooms."
    assert result["facts"]["rooms"] == 3 and result["facts"]["available"]
    assert Enquiry.load(world.repo, GUEST).rooms == 1  # only what the guest said is remembered, not the minimum
    again, _, _ = update(world, {"place": "Alleppey", "adults": 2})
    assert again["facts"]["rooms"] == 1  # 2 persons later are not forced into 3 rooms


def test_a_room_type_pick_is_checked_and_remembered(world):
    result, texts, _ = update(world, {**STAY, "adults": 2, "room_picks": [{"room_type": "family suite"}]})
    assert result["facts"]["rooms_offered"] == "1 Family Suite" and texts["room_picked"] == "Sure, Family Suite is free for your dates."
    assert "rooms_ok" not in texts  # 'Sure, X is free' replaces it
    later, _, _ = update(world, {"place": "Alleppey", "adults": 3})  # 3 adults: 2 rooms, still Family Suites
    assert later["facts"]["rooms_offered"] == "2 Family Suite"  # the pick survives a change of persons
    assert Enquiry.load(world.repo, GUEST).room_picks == [{"room_type": "Family Suite", "count": 1}]


def test_a_room_number_pick_finds_its_room_type(world):
    w = world
    w.family.room_list = ["201", "202", "203"]
    w.repo.save_room_type(w.family)
    result, _, _ = update(w, {**STAY, "adults": 2, "room_picks": [{"room_number": "202"}]})
    assert result["facts"]["rooms_offered"] == "1 Family Suite"
    bad, texts, _ = update(w, {**STAY, "adults": 2, "room_picks": [{"room_number": "999"}]})
    assert not bad["ok"] and "no room 999" in bad["facts"]["problem"] and texts == {}


def test_two_room_types_in_one_stay(world):
    result, _, _ = update(world, {**STAY, "adults": 2, "room_picks": [{"room_type": "Deluxe", "count": 1},
                                                                       {"room_type": "Family Suite", "count": 1}]})
    assert result["facts"]["rooms"] == 2 and result["facts"]["rooms_offered"] == "1 Deluxe + 1 Family Suite"
    assert result["facts"]["price"]["total"] == 24000


def test_a_room_type_the_guest_does_not_want_is_excluded(world):
    result, _, _ = update(world, {**STAY, "adults": 2, "exclude_room_types": ["Deluxe"]})
    assert result["facts"]["rooms_offered"] == "1 Family Suite"


# --------------------------------------------------------------------------- not available, picked room not free


def test_not_available_sends_rooms_none_with_the_free_dates(world):
    w = world
    block(w, w.deluxe, 2)
    block(w, w.family, 3)
    result, texts, _ = update(w, {**STAY, "adults": 2})
    assert_shape(result)
    assert result["ok"] and result["facts"]["available"] is False and result["next_step"] == "ask_details"
    assert result["sent"] == ["rooms_none_alt"] and texts["rooms_none_alt"].startswith(
        "Sorry, rooms are not available for your dates. Rooms are free on ")
    assert result["facts"]["free_dates"]
    again, texts2, _ = update(w, {**STAY, "adults": 2})
    assert again["sent"] == [] and texts2 == {}  # the same 'not available' is never said twice


def test_not_available_with_no_free_dates_is_the_plain_sorry(world):
    w = world
    for start in range(0, 30):  # every night for a month is taken
        d = date(2026, 11, 3) + timedelta(days=start)
        block(w, w.deluxe, 2, d, d + timedelta(days=1))
        block(w, w.family, 3, d, d + timedelta(days=1))
    result, texts, _ = update(w, {**STAY, "adults": 2})
    assert result["sent"] == ["rooms_none"] and texts["rooms_none"] == t("rooms_none", "en")


def test_a_picked_room_that_is_not_free_is_told_and_the_owners_priority_goes_on(world):
    w = world
    block(w, w.family, 3)
    result, texts, _ = update(w, {**STAY, "adults": 2, "room_picks": [{"room_type": "Family Suite"}]})
    assert result["sent"][0] == "room_pick_no"
    assert texts["room_pick_no"] == "Sorry, Family Suite is not free for 2 persons on your dates."
    assert result["facts"]["available"] and result["facts"]["rooms_offered"] == "1 Deluxe"
    assert Enquiry.load(w.repo, GUEST).room_picks == []  # the failed pick is forgotten


# --------------------------------------------------------------------------- change after the payment link


def test_a_change_after_the_link_closes_the_old_link_and_sends_a_new_summary_and_link(world):
    w = world
    hold(w)  # the old bot takes a guest to an open link: Deluxe x 1, 10-12 Nov, 2 persons, Asha Menon
    old = w.repo.list_bookings(phone=GUEST)[0]
    assert old.status == "hold" and old.payment_link_id
    result, texts, _ = update(w, {**STAY, "adults": 4})  # now 4 persons: 2 rooms
    assert_shape(result)
    assert result["ok"] and result["sent"][-2:] == ["summary", "pay"] and result["next_step"] == "pay"
    assert "rooms_ok" not in texts and "ask_contact" not in texts  # never the name ask: name and mobile are known
    assert texts["summary"].startswith("Following are the glimpse of your enquiry") and "Asha Menon" in texts["summary"]
    assert "2 Rooms" in texts["summary"] or "Room" in texts["summary"]
    assert texts["pay"].startswith("Please pay the advance using this link:")
    assert w.repo.get_booking(old.id).status == "expired" and old.payment_link_id in getattr(w.pay, "cancelled", [])
    live = [b for b in w.repo.list_bookings(phone=GUEST) if b.status == "hold"]
    assert len(live) == 1 and live[0].rooms == 2 and live[0].payment_link_url in texts["pay"]
    enq = Enquiry.load(w.repo, GUEST)
    assert enq.booking_id == live[0].id and enq.link_open and enq.next_step == "pay"


def test_the_same_stay_as_the_open_link_changes_nothing(world):
    w = world
    hold(w)
    old = w.repo.list_bookings(phone=GUEST)[0]
    result, texts, _ = update(w, {**STAY, "adults": 2})
    assert result["sent"] == [] and texts == {} and "unchanged" in result["note"]
    assert w.repo.get_booking(old.id).status == "hold" and not getattr(w.pay, "cancelled", [])


def test_a_change_that_is_not_free_leaves_the_open_link_alone(world):
    w = world
    hold(w)
    old = w.repo.list_bookings(phone=GUEST)[0]
    block(w, w.deluxe, 1, date(2026, 11, 20), date(2026, 11, 22))
    block(w, w.deluxe, 1, date(2026, 11, 20), date(2026, 11, 22))
    block(w, w.family, 3, date(2026, 11, 20), date(2026, 11, 22))
    result, _, _ = update(w, {"place": "Alleppey", "check_in": "2026-11-20", "check_out": "2026-11-22", "adults": 2})
    assert result["facts"]["available"] is False
    assert w.repo.get_booking(old.id).status == "hold"


# --------------------------------------------------------------------------- get_price


def price(w, args=None, phone=GUEST):
    tools, box = agent(w, phone)
    result = tools.get_price(args or {})
    return result, {i.key: i.text for i in box.queue}


def test_get_price_sends_the_owners_price_text_and_follows_the_dashboard_at_once(world):
    w = world
    update(w, {**STAY, "adults": 2})
    result, texts = price(w)
    assert_shape(result)
    assert result["ok"] and result["sent"] == ["price_answer"]
    assert texts["price_answer"] == ("Deluxe: ₹4,000 per room per night.\n"
                                     "1 room(s), Deluxe, 10 Nov to 12 Nov 2026 (2 nights): total ₹8,000.")
    assert result["facts"]["price"]["total"] == 8000
    w.deluxe.base_rate = 5000  # the owner changes the rate in the dashboard
    w.repo.save_room_type(w.deluxe)
    result, texts = price(w)
    assert result["facts"]["price"]["total"] == 10000 and "total ₹10,000" in texts["price_answer"]


def test_get_price_for_a_named_room_never_switches_the_stay(world):
    w = world
    update(w, {**STAY, "adults": 2})
    before = Enquiry.load(w.repo, GUEST)
    result, texts = price(w, {"room_type": "Family Suite"})
    assert result["facts"]["room_type"] == "Family Suite" and result["facts"]["price"]["total"] == 16000
    assert "Family Suite: ₹8,000 per room per night." in texts["price_answer"]
    assert Enquiry.load(w.repo, GUEST) == before and result["next_step"] == before.next_step


def test_get_price_on_a_room_that_is_not_free_says_so(world):
    w = world
    update(w, {**STAY, "adults": 2})
    block(w, w.family, 3)
    result, texts = price(w, {"room_type": "Family Suite"})
    assert result["facts"] == {"available": False} and texts["room_not_free"] == "Sorry, Family Suite is not free on your dates."


def test_get_price_with_no_stay_known_asks_for_the_details(world):
    result, texts = price(world)
    assert not result["ok"] and texts == {} and "update_enquiry" in result["note"]
    assert result["next_step"] == "ask_location"


def test_get_price_after_the_link_uses_the_stay_on_the_link_and_changes_nothing(world):
    w = world
    hold(w)
    old = w.repo.list_bookings(phone=GUEST)[0]
    result, texts = price(w, {"room_type": "Family Suite"})
    assert result["ok"] and result["facts"]["room_type"] == "Family Suite" and result["facts"]["price"]["total"] == 16000
    assert w.repo.get_booking(old.id).status == "hold" and len([b for b in w.repo.list_bookings(phone=GUEST)]) == 1
    plain, _ = price(w)
    assert plain["facts"]["room_type"] == "Deluxe" and plain["facts"]["price"]["total"] == 8000


# --------------------------------------------------------------------------- early check-in (a flag of update_enquiry)


def offer_early(w):
    w.p1.early_checkin_time, w.p1.early_checkin_rate = "10:00", 1500
    w.repo.save_property(w.p1)


def test_early_checkin_changes_the_room_check_price_and_is_remembered(world):
    w = world
    offer_early(w)
    plain, _, _ = update(w, {**STAY, "adults": 2})
    assert plain["facts"]["price"]["total"] == 8000 and "early_checkin_fee" not in plain["facts"]["price"]
    early, texts, _ = update(w, {**STAY, "adults": 2, "early_checkin": True})
    assert early["ok"] and early["facts"]["early_checkin"] is True
    assert early["facts"]["price"] == {"total": 9500, "advance_now": 4750, "balance_at_property": 4750,
                                       "per_room_per_night": 4000, "early_checkin_fee": 1500}
    assert Enquiry.load(w.repo, GUEST).early_checkin and "early check-in asked" in Enquiry.load(w.repo, GUEST).render()
    later, _, _ = update(w, {"place": "Alleppey", "adults": 3})  # a later change keeps it: 3 persons = 2 rooms
    assert later["facts"]["early_checkin"] is True and later["facts"]["price"]["early_checkin_fee"] == 3000  # per room
    off, _, _ = update(w, {"place": "Alleppey", "early_checkin": False})
    assert off["facts"]["early_checkin"] is False and off["facts"]["price"]["total"] == 16000  # 2 rooms, no fee


def test_early_checkin_not_offered_is_a_problem_for_the_ai(world):
    result, texts, _ = update(world, {**STAY, "adults": 2, "early_checkin": True})
    assert not result["ok"] and "not offered" in result["facts"]["problem"] and texts == {}
    assert not Enquiry.load(world.repo, GUEST).early_checkin


def test_early_checkin_is_in_the_get_price_answer_and_the_booking(world):
    w = world
    offer_early(w)
    update(w, {**STAY, "adults": 2, "early_checkin": True})
    tools, box = agent(w)
    assert tools.get_price({})["facts"]["price"]["total"] == 9500
    made = tools.create_booking({"guest_name": "Asha", "mobile": "same"})
    texts = {i.key: i.text for i in box.queue}
    assert made["ok"] and "Early Check-in Charges - 1,500" in texts["summary"]
    booking = w.repo.list_bookings(phone=GUEST)[0]
    assert booking.total == 9500 and booking.advance == 4750 and "Early check-in" in booking.notes


def test_changing_early_checkin_after_the_link_makes_a_new_link(world):
    w = world
    offer_early(w)
    hold(w)
    same, texts, _ = update(w, {**STAY, "adults": 2, "early_checkin": False})
    assert same["sent"] == []  # the same stay as the open link
    result, texts, _ = update(w, {**STAY, "adults": 2, "early_checkin": True})
    assert result["ok"] and result["sent"][-2:] == ["summary", "pay"] and "Early Check-in Charges - 1,500" in texts["summary"]
    assert len([b for b in w.repo.list_bookings(phone=GUEST) if b.status == "hold"]) == 1
