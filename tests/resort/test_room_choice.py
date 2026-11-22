"""The guest PICKS a room (owner, 2026-11-22, live chat): 'I need a tiny cottage room, book that', 'I only need
cottage', 'I don't want standard rooms', 'can I get room 103', 'I want other room'. The bot remembers the stay it
already checked and switches the room in code: never the dates form again."""

from __future__ import annotations

import pytest

from app.resort.models import RoomType
from test_bot import GUEST, reply, say, tool, world  # noqa: F401  (fixture)

FORM = "Please share these details:"


@pytest.fixture()
def resort(world):
    """Like the live resort: Standard first by priority, then the 2 Bedroom Tiny Kerala Cottage, then Basic."""
    w = world
    for rt in (w.deluxe, w.family):
        w.repo.save_room_type(RoomType(**{**rt.__dict__, "active": False}))
    w.std = w.repo.save_room_type(RoomType(w.p1.id, "Beach View Standard Rooms at First Floor", units=2, max_adults=3,
                                           max_children=3, max_occupancy=3, base_rate=3000, priority=1,
                                           room_list=["108", "109"]))
    w.cot = w.repo.save_room_type(RoomType(w.p1.id, "2 Bedroom Tiny Kerala Cottage", units=2, max_adults=3,
                                           max_children=1, max_occupancy=3, base_rate=4000, priority=2,
                                           room_list=["103", "104"]))
    w.basic = w.repo.save_room_type(RoomType(w.p1.id, "Beach View Basic Room", units=2, max_adults=3, max_children=1,
                                             max_occupancy=3, base_rate=3000, priority=3, room_list=["101", "102"]))
    say(w, "Alleppey 13 jan to 17 jan, 3 persons, 1 room", GUEST,
        tool("check_availability", place="Alleppey", check_in="2027-01-13", check_out="2027-01-17", adults=3,
             rooms=1),
        reply("[no reply]"))
    assert "Yes, rooms are available for your dates." in w.wa.texts()
    w.wa.sent.clear()
    return w


def booked(w):
    return [w.repo.get_room_type(b.room_type_id).name for b in w.repo.list_bookings(phone=GUEST, status="hold")]


def summary(w):
    return next(x for x in reversed(w.wa.texts()) if x.startswith("Following are the glimpse"))


@pytest.mark.parametrize("text", ["I need a tiny cottage room, okay? Book that.", "I only need cottage",
                                  "Ok book cottage", "cottage mathi", "can I get room 103?",
                                  "I don't want standard rooms"])
def test_after_the_payment_link_a_picked_room_gives_a_new_summary_never_the_form(resort, text):
    w = resort
    say(w, "Naseem, same number", GUEST, tool("create_booking", guest_name="Naseem", mobile="same"),
        reply("[no reply]"))
    assert "Beach View Standard Rooms at First Floor" in summary(w)
    w.wa.sent.clear()
    say(w, text)  # in code: the AI is not needed
    texts = w.wa.texts()
    assert not any(x.startswith(FORM) for x in texts), texts
    assert "2 Bedroom Tiny Kerala Cottage" in summary(w)
    assert "Check in - 13th Jan 27" in summary(w) and "Naseem" in summary(w)
    assert "https://rzp.io/" in texts[-1]  # the new link (in the guest's language)
    if text != "cottage mathi":  # English in, English out (strict rule); 'cottage mathi' is Manglish
        assert texts[-1].startswith("Please pay the advance using this link:"), texts[-1]
    assert booked(w) == ["2 Bedroom Tiny Kerala Cottage"]


@pytest.mark.parametrize("text", ["Ok book cottage", "I want the cottage", "tiny cottage", "can I get room 103?"])
def test_before_the_name_a_picked_room_is_checked_and_used(resort, text):
    w = resort
    say(w, text)
    texts = w.wa.texts()
    assert not any(x.startswith(FORM) for x in texts), texts
    assert "Sure, 2 Bedroom Tiny Kerala Cottage is free for your dates." in texts
    assert texts[-1] == "Please share your name and mobile number to proceed."
    say(w, "Naseem, same number", GUEST, tool("create_booking", guest_name="Naseem", mobile="same"),
        reply("[no reply]"))
    assert booked(w) == ["2 Bedroom Tiny Kerala Cottage"]


def test_i_want_other_room_gives_the_next_free_room_type(resort):
    w = resort
    say(w, "I want other room")
    assert "Sure, 2 Bedroom Tiny Kerala Cottage is free for your dates." in w.wa.texts()
    assert not any(x.startswith(FORM) for x in w.wa.texts())


def test_any_other_rooms_says_which_one_is_free_even_without_photos(resort):
    w = resort
    say(w, "any other rooms?")
    assert w.wa.texts()[0] == ("Yes, 2 Bedroom Tiny Kerala Cottage is also free for your dates. Tell me if you want "
                               "this room.")


def test_four_persons_who_only_want_the_cottage_get_two_cottages(resort):
    w = resort
    say(w, "Naseem, same number", GUEST, tool("create_booking", guest_name="Naseem", mobile="same"),
        reply("[no reply]"))
    say(w, "I only need cottage")
    w.wa.sent.clear()
    say(w, "4 persons")
    assert "2 Bedroom Tiny Kerala Cottage" in summary(w) and "2 Rooms" in summary(w)
    assert "Beach View Standard" not in summary(w)


def test_a_picked_room_that_cannot_take_the_group_is_said_plainly(resort):
    w = resort
    w.repo.save_room_type(RoomType(**{**w.repo.get_room_type(w.cot.id).__dict__, "units": 0}))
    say(w, "I only need cottage")
    texts = w.wa.texts()
    assert "Sorry, 2 Bedroom Tiny Kerala Cottage is not free for 3 persons on your dates." in texts
    assert not any(x.startswith(FORM) for x in texts)


def test_questions_and_photo_asks_are_not_a_room_choice(resort):
    w = resort
    st = w.repo.get_state(GUEST)
    for text in ("is the cottage free?", "cottage photos venam", "I need a room in kerala"):
        assert w.bot._room_choice_now(GUEST, dict(st), text) is None, text


def test_is_there_any_other_room_available_names_the_free_one(resort):
    w = resort
    say(w, "Is there any other room available?")
    assert w.wa.texts()[0].startswith("Yes, 2 Bedroom Tiny Kerala Cottage is also free for your dates.")


def test_asking_again_for_the_room_already_in_the_link_sends_no_new_link(resort):
    w = resort
    say(w, "Naseem, same number", GUEST, tool("create_booking", guest_name="Naseem", mobile="same"),
        reply("[no reply]"))
    say(w, "I only need cottage")
    w.wa.sent.clear()
    say(w, "I don't want standard rooms")
    assert w.wa.texts() == ["Sure, 2 Bedroom Tiny Kerala Cottage is free for your dates."]
    assert booked(w) == ["2 Bedroom Tiny Kerala Cottage"]


def test_a_room_choice_question_is_answered_in_code_only(resort):
    w = resort
    say(w, "Naseem, same number", GUEST, tool("create_booking", guest_name="Naseem", mobile="same"),
        reply("[no reply]"))
    calls = len(w.llm.calls)
    w.wa.sent.clear()
    say(w, "can I get room 103?")
    assert len(w.llm.calls) == calls  # the AI was not asked to add anything (it once sent the form too)
    assert not any(x.startswith(FORM) for x in w.wa.texts())


# ---- the AI's change_enquiry tool: ANY change in any words, after the payment link (owner, 2026-11-22)

@pytest.mark.parametrize("change, room, rooms, nights", [
    ({"room_type": "2 Bedroom Tiny Kerala Cottage"}, "2 Bedroom Tiny Kerala Cottage", "1 Room", "4 Nights"),
    ({"room_number": "101"}, "Beach View Basic Room", "1 Room", "4 Nights"),
    ({"remove_room_type": "Beach View Standard Rooms at First Floor"}, "2 Bedroom Tiny Kerala Cottage", "1 Room",
     "4 Nights"),
    ({"other_room": True}, "2 Bedroom Tiny Kerala Cottage", "1 Room", "4 Nights"),
    ({"add_rooms": 1}, "Beach View Standard Rooms at First Floor", "2 Rooms", "4 Nights"),
    # 2 per room (owner's rule): 5 persons = 3 rooms; only 2 Standard rooms exist, so the cottage joins
    ({"adults": 5}, "2 Beach View Standard Rooms at First Floor + 1 2 Bedroom Tiny Kerala Cottage", "3 Rooms",
     "4 Nights"),
    ({"check_in": "2027-01-20", "check_out": "2027-01-22"}, "Beach View Standard Rooms at First Floor", "1 Room",
     "2 Nights"),
])
def test_the_ai_can_make_any_change_after_the_link_and_a_new_link_goes(resort, change, room, rooms, nights):
    w = resort
    say(w, "Naseem, same number", GUEST, tool("create_booking", guest_name="Naseem", mobile="same"),
        reply("[no reply]"))
    w.wa.sent.clear()
    say(w, "pls change it like I said", GUEST, tool("change_enquiry", **change), reply("[no reply]"))
    texts = w.wa.texts()
    assert not any(x.startswith(FORM) for x in texts), texts
    s = summary(w)
    assert f"*{room}*" in s and rooms in s and nights in s and "Naseem" in s, s
    assert texts[-1].startswith("Please pay the advance using this link:")
    assert len({b.payment_link_id for b in w.repo.list_bookings(phone=GUEST, status="hold")}) == 1  # old link closed


def test_the_ai_can_change_the_room_before_the_name(resort):
    w = resort
    say(w, "the bigger one with 2 bedrooms", GUEST, tool("change_enquiry", room_type="cottage"), reply("[no reply]"))
    assert "Sure, 2 Bedroom Tiny Kerala Cottage is free for your dates." in w.wa.texts()
    say(w, "Naseem, same number", GUEST, tool("create_booking", guest_name="Naseem", mobile="same"),
        reply("[no reply]"))
    assert booked(w) == ["2 Bedroom Tiny Kerala Cottage"]


def test_the_prompt_lets_guests_choose_and_remembers_the_enquiry(resort):
    w = resort
    say(w, "Naseem, same number", GUEST, tool("create_booking", guest_name="Naseem", mobile="same"),
        reply("[no reply]"))
    say(w, "hmm", GUEST, reply("[no reply]"))
    system = w.llm.calls[-1].system
    assert "may choose ANY room type" in system and "room numbers 103, 104" in system
    assert "CURRENT ENQUIRY" in system and "check-in 2027-01-13" in system and "Payment link: SENT" in system


def test_room_numbers_never_told_unless_the_guest_named_one():
    """Owner, 2026-11-22: 'is there 2 rooms in kerala cottage' -> 'Yes, 2 rooms available.', not 'Room numbers 103 and 104.'"""
    from app.resort.chat import ChatMixin
    ask = "is there 2 rooms in kerala cottage"
    said = "Yes, 2 rooms available in Kerala Cottage. Room numbers 103 and 104."
    assert ChatMixin._reply_problem(said, ask, {}).startswith("Rewrite that reply without any room numbers")
    assert ChatMixin._without_room_numbers(said, ask) == "Yes, 2 rooms available in Kerala Cottage."
    assert ChatMixin._without_room_numbers("Rooms 103/104 are free.", ask) == "Rooms 103/104 are free."  # nothing else left
    assert ChatMixin._without_room_numbers("Yes, room 103 is free.", "is room 103 free?") == "Yes, room 103 is free."
    assert ChatMixin._without_room_numbers("Your rooms are 103 and 104.", "what is my room number?") == "Your rooms are 103 and 104."
    assert ChatMixin._reply_problem("Yes, 2 rooms available. ₹3,500 per night.", ask, {}) == ""



@pytest.mark.parametrize("text", ["is there 2 rooms in kerala cottage", "is there 2 rooms in kerala cottage?",
                                  "I want 2 rooms in the cottage"])
def test_is_there_2_rooms_in_the_cottage_gives_the_cottage_and_no_room_numbers(resort, text):
    """Live chat 2026-11-22: 'is there 2 rooms in kerala cottage' got 'Room numbers 103 and 104.' (not the guest's
    concern), and the code read '2 rooms' but kept the Standard room."""
    w = resort
    say(w, text, GUEST, reply("Yes, 2 rooms available in Kerala Cottage. Room numbers 103 and 104."),
        reply("Yes, 2 rooms available in Kerala Cottage."))
    texts = w.wa.texts()
    assert not any("103" in x or "104" in x for x in texts), texts
    assert "Sure, 2 Bedroom Tiny Kerala Cottage is free for your dates." in texts
    assert "For 3 persons: 2 room(s)." in texts
    w.llm.script.clear()
    say(w, "Naseem, same number", GUEST, tool("create_booking", guest_name="Naseem", mobile="same"), reply("[no reply]"))
    holds = w.repo.list_bookings(phone=GUEST, status="hold")
    assert {w.repo.get_room_type(b.room_type_id).name for b in holds} == {"2 Bedroom Tiny Kerala Cottage"}
