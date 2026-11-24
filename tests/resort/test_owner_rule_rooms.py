"""Owner rule (2026-11-24), for BOTH the old bot and the agent:
- persons are ADULTS above 6 years only; children are not asked and not counted;
- 2 adults per room by default (the fewest rooms); if the guest chooses fewer rooms, up to 3 adults in one room, never more;
- an extra bed is never added by itself: it is asked ONCE ('Do you need an extra bed?'), only when a room will hold
  3 adults; yes -> charged at the extra-bed rate (per bed per night), no -> nothing; a bed the guest asks for is charged.
Rates in the test world: Deluxe 4000 (2 rooms), Family Suite 8000 (3 rooms), extra bed 500 per bed per night, 2 nights."""

from __future__ import annotations

import json
from datetime import date

import pytest
from test_bot import GUEST, confirm, last_tool, reply, say, tool, world  # noqa: F401  (world is a fixture)

from app.resort import availability as av
from app.resort.agent.outbox import Outbox
from app.resort.agent.prompt import build_system_prompt
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import TOOL_SCHEMAS, AgentTools
from app.resort.chat import parse_stay
from app.resort.i18n import HN, MN, S, TN, t
from app.resort.models import RoomType

STAY = {"place": "Alleppey", "check_in": "2026-11-10", "check_out": "2026-11-12"}
QUESTION = "Do you need an extra bed?"
NEW_FORM = "Please share these details:\n\nCheck in :\nCheck out :\nNumber of adults (above 6 years) :\nNumber of rooms:"


def agent(w, args, call="update_enquiry"):
    box = Outbox(GUEST, dry_run=True)
    result = AgentTools(w.bot, GUEST, box).call(call, args)
    return result, {i.key: i.text for i in box.queue}


def update(w, **args):
    return agent(w, {**STAY, **args})


# --------------------------------------------------------------------------- the pure rule (availability.py)


@pytest.mark.parametrize("adults, rooms, threes", [(3, 1, 1), (3, 2, 0), (4, 2, 0), (5, 2, 1), (6, 2, 2), (6, 3, 0),
                                                   (7, 3, 1), (9, 3, 3), (2, 1, 0), (1, 1, 0), (4, 1, 1), (0, 1, 0)])
def test_rooms_with_three_adults(adults, rooms, threes):
    assert av.rooms_with_three(adults, rooms) == threes


def test_two_per_room_by_default_three_when_fewer_rooms_are_chosen_never_more(world):
    rt = world.deluxe
    assert av.base_per_room(rt) == 2 and av.room_cap(rt) == 3
    assert av.plan_rooms(rt, 3, 0, 2) == (2, 0)  # 2 per room, no extra bed planned
    assert av.plan_rooms(rt, 3, 0, 2, wanted=1) == (1, 0)  # fewer rooms: 3 adults in one
    assert av.plan_rooms(rt, 4, 0, 2, wanted=1) is None  # max 3 adults per room
    assert av.plan_rooms(rt, 5, 0, 2, wanted=2) == (2, 0) and av.plan_rooms(rt, 7, 0, 2, wanted=2) is None
    big = RoomType(rt.property_id, "Suite", units=3, max_adults=6, max_occupancy=6)  # a big room is still 3 at most
    assert av.room_cap(big) == 3 and av.plan_rooms(big, 4, 0, 3, wanted=1) is None
    small = RoomType(rt.property_id, "Single", units=3, max_adults=1, max_occupancy=1)
    assert av.base_per_room(small) == 1 and av.room_cap(small) == 1
    assert av.fewest_rooms([rt], 4, 0) == 2 and av.fewest_rooms([rt], 9, 0) == 3 and av.fewest_rooms([], 4, 0) == 0


def test_children_are_not_counted(world):
    rt = world.deluxe
    assert av.plan_rooms(rt, 2, 5, 2) == (1, 0)  # 5 children change nothing
    assert av.fewest_rooms([rt], 3, 9) == 1


# --------------------------------------------------------------------------- the agent


def test_agent_3_adults_no_room_count_is_2_rooms_and_no_question(world):
    result, texts = update(world, adults=3)
    assert result["facts"]["rooms"] == 2 and result["facts"]["extra_beds"] == 0
    assert result["facts"]["extra_bed_question"] is False and "ask_extra_bed" not in texts
    assert result["next_step"] == "ask_contact" and result["sent"][-1] == "ask_contact"


def test_agent_3_adults_2_rooms_is_2_rooms_and_no_question(world):
    result, texts = update(world, adults=3, rooms=2)
    assert result["facts"]["rooms"] == 2 and "ask_extra_bed" not in texts and result["next_step"] == "ask_contact"


def test_agent_3_adults_1_room_asks_the_question_and_waits(world):
    w = world
    result, texts = update(w, adults=3, rooms=1)
    assert result["facts"]["rooms"] == 1 and result["facts"]["rooms_offered"] == "1 Deluxe"
    assert result["sent"] == ["rooms_ok", "rooms_plain", "ask_extra_bed"] and texts["ask_extra_bed"] == QUESTION
    assert "ask_contact" not in texts and result["next_step"] == "ask_extra_bed" and result["facts"]["extra_bed_question"]
    assert result["facts"]["price"]["total"] == 8000  # nothing added by itself
    # the booking waits for the answer
    blocked, texts = agent(w, {"guest_name": "Asha", "mobile": "same"}, "create_booking")
    assert not blocked["ok"] and "not answered" in blocked["facts"]["problem"] and texts == {}
    assert w.repo.list_bookings(phone=GUEST) == [] and blocked["next_step"] == "ask_extra_bed"
    # asked once: the same stay again does not ask again
    again, texts = update(w, adults=3, rooms=1)
    assert again["sent"] == [] and again["next_step"] == "ask_extra_bed"


def test_agent_yes_adds_the_extra_bed_charged_per_bed_per_night(world):
    w = world
    update(w, adults=3, rooms=1)
    result, texts = update(w, adults=3, rooms=1, extra_beds=1)
    assert result["sent"] == ["rooms_beds", "ask_contact"] and result["next_step"] == "ask_contact"
    assert texts["rooms_beds"] == "For 3 persons: 1 room(s) + 1 extra bed(s) (₹500 per extra bed per night)."
    assert result["facts"]["extra_beds"] == 1 and result["facts"]["price"]["total"] == 9000  # 8000 + 500 x 1 bed x 2 nights
    made, texts = agent(w, {"guest_name": "Asha", "mobile": "same"}, "create_booking")
    assert made["ok"] and "Extra Bed - 1,000" in texts["summary"] and "Total Cost - 9,000" in texts["summary"]
    booking = w.repo.list_bookings(phone=GUEST)[0]
    assert booking.total == 9000 and "Extra beds: 1" in booking.notes


def test_agent_no_keeps_the_room_as_is_with_no_charge(world):
    w = world
    update(w, adults=3, rooms=1)
    result, texts = update(w, adults=3, rooms=1, extra_beds=0)
    assert result["sent"] == ["ask_contact"] and result["next_step"] == "ask_contact"
    assert result["facts"]["extra_beds"] == 0 and result["facts"]["price"]["total"] == 8000
    made, texts = agent(w, {"guest_name": "Asha", "mobile": "same"}, "create_booking")
    assert made["ok"] and "Extra Bed" not in texts["summary"] and w.repo.list_bookings(phone=GUEST)[0].total == 8000


def test_agent_4_adults_1_room_is_not_allowed(world):
    result, texts = update(world, adults=4, rooms=1)
    assert result["sent"][0] == "min_rooms" and texts["min_rooms"] == "Max 3 persons per room, so 4 persons need at least 2 rooms."
    assert result["facts"]["rooms"] == 2 and "ask_extra_bed" not in texts  # 2 + 2: no room holds 3


def test_agent_5_adults_2_rooms_asks_the_question_once(world):
    w = world
    result, texts = update(w, adults=5, rooms=2)
    assert result["facts"]["rooms"] == 2 and result["sent"][-1] == "ask_extra_bed" and result["next_step"] == "ask_extra_bed"
    again, _ = update(w, adults=5, rooms=2)
    assert again["sent"] == []  # never twice
    answered, texts = update(w, adults=5, rooms=2, extra_beds=1)  # one room has 3 adults
    assert answered["facts"]["price"]["total"] == 16000 + 1000 and answered["sent"] == ["rooms_beds", "ask_contact"]


HOW_MANY = "How many extra beds do you need?"


def test_agent_3_adults_in_1_room_asks_yes_no_not_how_many(world):
    result, texts = update(world, adults=3, rooms=1)
    assert texts["ask_extra_bed"] == QUESTION and "ask_extra_beds" not in texts and result["facts"]["extra_beds_max"] == 1


def test_agent_10_adults_in_4_rooms_asks_how_many(world):
    result, texts = update(world, adults=10, rooms=4)  # 3 + 3 + 2 + 2: two rooms hold 3 adults
    assert result["facts"]["rooms"] == 4 and result["facts"]["extra_beds_max"] == 2
    assert texts["ask_extra_beds"] == HOW_MANY and "ask_extra_bed" not in texts and "ask_contact" not in texts
    assert result["next_step"] == "ask_extra_bed"
    again, _ = update(world, adults=10, rooms=4)
    assert again["sent"] == []  # asked once
    assert Enquiry.load(world.repo, GUEST).checked["extra_bed_max"] == 2


@pytest.mark.parametrize("answer, beds, capped", [(2, 2, False), (1, 1, False), (0, 0, False), (5, 2, True)])
def test_agent_how_many_answer(world, answer, beds, capped):
    base = update(world, adults=10, rooms=4)[0]["facts"]["price"]["total"]
    result, texts = update(world, adults=10, rooms=4, extra_beds=answer)
    assert result["ok"] and result["facts"]["extra_beds"] == beds
    assert result["facts"]["price"]["total"] == base + 500 * 2 * beds  # 500 per bed per night, 2 nights
    assert result["next_step"] == "ask_contact" and ("extra_beds_max" in texts) is capped
    if capped:
        assert texts["extra_beds_max"] == "The maximum is 2 extra bed(s) for your rooms, so 2 added."


def test_agent_how_many_no_means_none(world):
    update(world, adults=10, rooms=4)
    result, _ = update(world, adults=10, rooms=4, extra_beds=0)  # the AI passes 0 for "no"
    assert result["facts"]["extra_beds"] == 0 and result["next_step"] == "ask_contact"


def test_how_many_question_is_in_every_language(world):
    assert t("ask_extra_beds", "en") == HOW_MANY
    assert MN["ask_extra_beds"] == "Ethra extra bed venam?" and TN["ask_extra_beds"] == "Evvalavu extra bed venum?"
    assert HN["ask_extra_beds"] == "Aapko kitne extra bed chahiye?"
    result, texts = update(world, adults=10, rooms=4, language="manglish")
    assert texts["ask_extra_beds"] == "Ethra extra bed venam?"


def test_agent_a_bed_the_guest_asks_for_is_added_and_charged_without_a_question(world):
    w = world
    result, texts = update(w, adults=2, extra_beds=1)  # 2 adults, 1 room, they want a bed
    assert result["facts"]["extra_beds"] == 1 and result["facts"]["price"]["total"] == 9000
    assert "ask_extra_bed" not in texts and result["sent"] == ["rooms_ok", "rooms_beds", "ask_contact"]
    too_many, texts = update(w, adults=2, extra_beds=2)  # at most one per room
    assert not too_many["ok"] and "at most one extra bed per room" in too_many["facts"]["problem"] and texts == {}


def test_agent_a_new_layout_asks_again_but_the_same_layout_keeps_the_answer(world):
    w = world
    update(w, adults=3, rooms=1)
    update(w, adults=3, rooms=1, extra_beds=1)
    moved, texts = update(w, adults=3, rooms=1, check_in="2026-11-20", check_out="2026-11-22")  # other dates, same layout
    assert moved["facts"]["extra_beds"] == 1 and "ask_extra_bed" not in texts
    new_layout, texts = update(w, adults=5, rooms=2, check_in="2026-11-20", check_out="2026-11-22")
    assert new_layout["facts"]["extra_beds"] == 0 and new_layout["sent"][-1] == "ask_extra_bed"


def test_agent_a_resort_with_no_extra_beds_never_asks(world):
    w = world
    w.p1.extra_bed_rate = 0
    w.repo.save_property(w.p1)
    result, texts = update(w, adults=3, rooms=1)
    assert "ask_extra_bed" not in texts and result["next_step"] == "ask_contact"
    no_bed, texts = update(w, adults=3, rooms=1, extra_beds=1)
    assert not no_bed["ok"] and "not offered" in no_bed["facts"]["problem"]


def test_agent_the_question_is_in_every_language(world):
    assert t("ask_extra_bed", "en") == QUESTION
    assert MN["ask_extra_bed"] == "Extra bed venamo?" and TN["ask_extra_bed"] == "Extra bed venumaa?"
    assert HN["ask_extra_bed"] == "Kya aapko extra bed chahiye?"
    assert t("ask_extra_bed", "ml") == t("ask_extra_bed", "mn") and t("ask_extra_bed", "hi") == t("ask_extra_bed", "hn")
    result, texts = update(world, adults=3, rooms=1, language="manglish")
    assert texts["ask_extra_bed"] == "Extra bed venamo?"


def test_agent_a_layout_change_after_the_link_closes_the_old_link_at_once_then_a_new_link_after_the_answer(world):
    from test_bot import hold
    w = world
    hold(w)  # Deluxe x 1, 2 adults, link open, Asha Menon
    old = w.repo.list_bookings(phone=GUEST)[0]
    assert old.status == "hold" and old.payment_link_id
    result, texts = update(w, adults=3, rooms=1)  # 3 adults in the one room: the question
    assert result["sent"][-1] == "ask_extra_bed" and "pay" not in texts and "summary" not in texts
    # the old link is closed AT ONCE, before the answer ('old link closed on any change'): it can no longer be paid
    assert w.repo.get_booking(old.id).status == "expired" and old.payment_link_id in w.pay.cancelled
    assert not [b for b in w.repo.list_bookings(phone=GUEST) if b.status == "hold"]
    assert "closed" in result["note"] and result["next_step"] == "ask_extra_bed"
    enq = Enquiry.load(w.repo, GUEST)
    assert enq.booking_id is None and not enq.link_open and enq.contact["name"] == "Asha Menon"
    again, texts = update(w, adults=3, rooms=1)  # asking again changes nothing, nothing is made
    assert again["sent"] == [] and len(w.pay.cancelled) == 1
    # the answer: a new summary and link go at once, with the name and mobile already known
    done, texts = update(w, adults=3, rooms=1, extra_beds=1)
    assert done["sent"][-2:] == ["summary", "pay"] and "Guest - Asha Menon" in texts["summary"]
    assert "Extra Bed - 1,000" in texts["summary"] and "ask_contact" not in texts
    live = [b for b in w.repo.list_bookings(phone=GUEST) if b.status == "hold"]
    assert len(live) == 1 and live[0].id != old.id and live[0].total == 9000 and "Extra beds: 1" in live[0].notes
    after = Enquiry.load(w.repo, GUEST)
    assert after.booking_id == live[0].id and after.link_open and after.contact is None and done["next_step"] == "pay"


def test_agent_a_layout_change_after_the_link_and_the_answer_no_makes_a_new_link_without_a_bed(world):
    from test_bot import hold
    w = world
    hold(w)
    old = w.repo.list_bookings(phone=GUEST)[0]
    update(w, adults=3, rooms=1)
    done, texts = update(w, adults=3, rooms=1, extra_beds=0)
    assert done["sent"][-2:] == ["summary", "pay"] and "Extra Bed" not in texts["summary"]
    assert [b for b in w.repo.list_bookings(phone=GUEST) if b.status == "hold"][0].total == 8000
    assert w.repo.get_booking(old.id).status == "expired"


def test_agent_a_change_that_needs_no_question_still_relinks_at_once(world):
    from test_bot import hold
    w = world
    hold(w)
    old = w.repo.list_bookings(phone=GUEST)[0]
    result, texts = update(w, adults=4)  # 2 + 2: no room holds 3, no question
    assert result["sent"][-2:] == ["summary", "pay"] and w.repo.get_booking(old.id).status == "expired"


def test_agent_the_same_stay_as_the_open_link_closes_nothing(world):
    from test_bot import hold
    w = world
    hold(w)
    old = w.repo.list_bookings(phone=GUEST)[0]
    result, texts = update(w, adults=2)
    assert result["sent"] == [] and w.repo.get_booking(old.id).status == "hold" and not getattr(w.pay, "cancelled", [])


# --------------------------------------------------------------------------- the form: adults only, no children anywhere


def test_the_form_asks_for_adults_above_6_years(world):
    w = world
    assert t("form", "en") == NEW_FORM and all(t("form", lang) == NEW_FORM for lang in ("ml", "ta", "hi", "mn", "tn", "hn"))
    result, texts = agent(w, {"place": "Alleppey"})
    assert texts["form"] == NEW_FORM
    result, texts = agent(w, {"place": "Alleppey", "check_in": "2026-11-10"})  # missing-lines logic is the same
    assert texts["form"] == "Please share these details:\n\nCheck out :\nNumber of adults (above 6 years) :\nNumber of rooms:"
    result, texts = agent(w, {"place": "Alleppey", "check_in": "2026-11-10", "check_out": "2026-11-12"})
    assert texts["form"] == "Please share these details:\n\nNumber of adults (above 6 years) :\nNumber of rooms:"


def test_no_children_question_anywhere(world):
    w = world
    for spec in TOOL_SCHEMAS:
        assert "children" not in spec.parameters["properties"], spec.name
    assert '"children":' not in json.dumps([s.parameters for s in TOOL_SCHEMAS])  # no field, only the rule
    asks = ("form", "ask_contact", "ask_name", "ask_extra_bed", "rooms_plain", "rooms_note", "rooms_beds", "beds_offer",
            "min_rooms", "rooms_most", "rooms_ok", "rooms_none", "fill_form", "contact_again")
    for table in (S, {k: {"x": v} for k, v in MN.items()}, {k: {"x": v} for k, v in TN.items()},
                  {k: {"x": v} for k, v in HN.items()}):
        for key in asks:
            for text in table.get(key, {}).values():
                assert "child" not in text.lower() and "kid" not in text.lower(), (key, text)
    prompt = build_system_prompt(w.bot, GUEST, Enquiry())
    assert "ADULTS above 6 years" in prompt and "never ask about or count children" in prompt
    assert "Number of children" not in prompt and "Number of persons" not in prompt
    result, texts = update(w, adults=2)
    assert "children" not in result["facts"] and "child" not in json.dumps(result).lower()


def test_the_rooms_sentences_never_say_a_third_person_needs_an_extra_bed():
    for table in (S, {k: {"x": v} for k, v in MN.items()}, {k: {"x": v} for k, v in TN.items()},
                  {k: {"x": v} for k, v in HN.items()}):
        for key in ("rooms_note", "beds_offer", "min_rooms", "rooms_most", "rooms_plain"):
            for text in table.get(key, {}).values():
                assert "extra" not in text.lower() and "2 + 1" not in text and "₹" not in text, (key, text)
    assert "up to 3 per room" in t("rooms_note", "en", persons=3, rooms=2, fewer=1)
    assert t("beds_offer", "en", persons=5, rooms=2).startswith("5 persons fit in 2 rooms (up to 3 per room).")


# --------------------------------------------------------------------------- the old bot


def old_check(w, adults, rooms=None, text=""):
    args = {**{"place": "Alleppey", "check_in": "2026-11-10", "check_out": "2026-11-12", "adults": adults}}
    if rooms:
        args["rooms"] = rooms
    say(w, f"2 nights from 10 Nov, {adults} people{text}", GUEST, tool("check_availability", **args), reply("[no reply]"))
    return last_tool(w)


def test_old_bot_3_adults_no_room_count_is_2_rooms_and_no_question(world):
    w = world
    res = old_check(w, 3)
    assert res["options"][0]["quoted_for_rooms"] == 2 and "extra_beds" not in res["options"][0]
    assert QUESTION not in w.wa.texts() and w.wa.texts()[-1] == "Please share your name and mobile number to proceed."
    assert any(x.startswith("For 3 persons, 2 rooms are needed (2 per room).") for x in w.wa.texts())


def test_old_bot_3_adults_2_rooms_is_2_rooms_and_no_question(world):
    w = world
    res = old_check(w, 3, rooms=2)
    assert res["options"][0]["quoted_for_rooms"] == 2 and QUESTION not in w.wa.texts()


def test_old_bot_3_adults_1_room_asks_the_question_then_yes_is_charged(world):
    w = world
    res = old_check(w, 3, rooms=1)
    assert res["options"][0]["quoted_for_rooms"] == 1 and res["options"][0]["total_for_stay"] == 8000
    assert w.wa.texts()[-1] == QUESTION and w.wa.texts().count(QUESTION) == 1
    assert "Please share your name and mobile number to proceed." not in w.wa.texts()
    say(w, "yes", GUEST, tool("prepare_booking", room_type="Deluxe", rooms=1, extra_beds=1), reply("[no reply]"))
    assert last_tool(w)["extra_beds"] == 1 and last_tool(w)["total"] == 9000  # 8000 + 500 x 1 bed x 2 nights
    confirm(w)
    booking = w.repo.list_bookings(phone=GUEST)[0]
    assert booking.total == 9000 and "Extra beds: 1" in booking.notes
    assert "Extra Bed - 1,000" in [x for x in w.wa.texts() if x.startswith("Following are the glimpse of your enquiry")][0]


def test_old_bot_3_adults_1_room_no_means_no_charge(world):
    w = world
    old_check(w, 3, rooms=1)
    say(w, "no thanks", GUEST, tool("prepare_booking", room_type="Deluxe", rooms=1), reply("[no reply]"))
    assert last_tool(w)["extra_beds"] == 0 and last_tool(w)["total"] == 8000
    confirm(w)
    booking = w.repo.list_bookings(phone=GUEST)[0]
    assert booking.total == 8000 and "Extra beds" not in booking.notes


def test_old_bot_4_adults_1_room_is_not_allowed(world):
    w = world
    res = old_check(w, 4, rooms=1)
    assert "Max 3 persons per room, so 4 persons need at least 2 rooms." in w.wa.texts()
    assert res["options"][0]["quoted_for_rooms"] == 2 and QUESTION not in w.wa.texts()


def test_old_bot_5_adults_2_rooms_asks_the_question_once(world):
    w = world
    res = old_check(w, 5, rooms=2)
    assert res["options"][0]["quoted_for_rooms"] == 2 and w.wa.texts().count(QUESTION) == 1
    old_check(w, 5, rooms=2, text=" again")  # the same stay again: never asked twice
    assert w.wa.texts().count(QUESTION) == 1


def test_old_bot_a_bed_the_guest_asks_for_is_charged(world):
    w = world
    old_check(w, 2)
    say(w, "one extra bed please", GUEST, tool("prepare_booking", room_type="Deluxe", rooms=1, extra_beds=1),
        reply("[no reply]"))
    assert last_tool(w)["extra_beds"] == 1 and last_tool(w)["total"] == 9000


def test_old_bot_the_form_asks_for_adults_and_a_reply_to_it_is_read(world):
    w = world
    assert w.bot._missing_form("en", {"check_in": True}) == (
        "Please share these details:\n\nCheck out :\nNumber of adults (above 6 years) :\nNumber of rooms:")
    today = date(2026, 11, 2)
    got = parse_stay("Check in : 10 Nov\nCheck out : 12 Nov\nNumber of adults (above 6 years) : 3\nNumber of rooms: 2", today)
    assert got["persons"] == 3 and got["rooms"] == 2 and "children" not in got
    old = parse_stay("Check in : 10 Nov\nCheck out : 12 Nov\nNumber of persons : 4", today)
    assert old["persons"] == 4  # a guest who copies the old wording is still understood


def test_old_bot_children_are_not_counted(world):
    got = parse_stay("12 dec to 14 dec, 2 adults and 2 kids", date(2026, 11, 2))
    assert got["persons"] == 2 and "children" not in got
    w = world
    say(w, "10 nov 2 nights 2 adults 1 child", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=2, adults=2, children=3),
        reply("[no reply]"))
    assert last_tool(w)["children"] == 0 and last_tool(w)["options"][0]["quoted_for_rooms"] == 1


def test_old_bot_system_prompt_has_the_new_rule(world):
    w = world
    say(w, "do you allow pets?", GUEST, reply("Sorry, I am not sure about pets."))
    system = w.llm.calls[0].system
    assert "ADULTS above 6 years only" in system and "'Do you need an extra bed?' once" in system
    assert "NEVER added by itself" in system and "2 + 1" not in system and "the 3rd on an extra bed" not in system
    assert "Number of adults above 6 years" in system
