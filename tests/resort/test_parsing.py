"""Unit tests for the code the resort eval leans on (Resort Step 2): dates, room capacity, language detection."""

from __future__ import annotations

from datetime import date

import pytest

from app.resort.availability import check_dates, min_rooms, room_options
from app.resort.chat import ChatMixin, latin_style, parse_stay
from app.resort.models import RoomType

TODAY = date(2026, 11, 18)


# --------------------------------------------------------------------------- dates


@pytest.mark.parametrize("text, ci, co, persons", [
    ("Check in :dec 12\nCheck out :dec 15\nNumber of persons :3", date(2026, 12, 12), date(2026, 12, 15), 3),
    ("dec 20 nu 2 per\n22 vare", date(2026, 12, 20), date(2026, 12, 22), 2),  # Manglish 'until'
    ("dec 20 la 2 per\n22 varai", date(2026, 12, 20), date(2026, 12, 22), 2),  # Tanglish
    ("ഡിസംബർ 12 മുതൽ 15 വരെ 2 പേർ", date(2026, 12, 12), date(2026, 12, 15), 2),  # Malayalam script
    ("டிசம்பர் 12 முதல் 15 வரை 2 பேர்", date(2026, 12, 12), date(2026, 12, 15), 2),  # Tamil script
    ("25/12 2 nights 4 pax", date(2026, 12, 25), date(2026, 12, 27), 4),
])
def test_stay_details_in_any_language(text, ci, co, persons):
    assert parse_stay(text, TODAY) == {"check_in": ci, "check_out": co, "persons": persons}


def test_a_date_already_past_this_year_means_next_year():
    got = parse_stay("10 nov to 12 nov", TODAY)
    assert got["check_in"] == date(2027, 11, 10) and got["check_out"] == date(2027, 11, 12)


def test_check_out_before_check_in_is_refused():
    assert check_dates(date(2026, 12, 12), date(2026, 12, 10), TODAY) == "check-out must be after check-in"
    assert check_dates(date(2026, 11, 1), date(2026, 11, 3), TODAY) == "check-in is in the past"
    assert check_dates(date(2026, 12, 12), date(2026, 12, 14), TODAY) is None


def test_a_persons_number_is_never_read_as_a_day():
    assert parse_stay("is Alleppey nice in december\n2 people", TODAY) == {"persons": 2}


# --------------------------------------------------------------------------- capacity


ROOM = RoomType("P", "Deluxe", units=5, max_adults=2, max_children=1, max_occupancy=3, base_rate=3000)


@pytest.mark.parametrize("adults, children, rooms", [(2, 0, 1), (3, 0, 2), (2, 1, 1), (4, 2, 2), (6, 0, 3)])
def test_fewest_rooms_for_a_party(adults, children, rooms):
    assert min_rooms(ROOM, adults, children) == rooms


def test_room_options_respect_free_rooms_and_one_adult_per_room():
    assert room_options(ROOM, 6, 0, 5) == [3, 4, 5]
    assert room_options(ROOM, 6, 0, 2) == []  # not enough rooms free
    assert room_options(ROOM, 1, 0, 5) == [1]  # never 2 rooms for one adult


def test_children_need_a_room_type_that_takes_children():
    assert min_rooms(RoomType("P", "Couple", max_children=0), 2, 1) is None


# --------------------------------------------------------------------------- language


@pytest.mark.parametrize("text, style", [("Do you have a room for 2?", "en"), ("room undo", "manglish"),
                                         ("ethra aanu rate?", "manglish")])
def test_latin_style(text, style):
    assert latin_style(text, "en") == style


@pytest.mark.parametrize("text, style", [("Saar room irukka? price evlo?", "tanglish"),
                                         ("enakku oru room venum", "tanglish"), ("enikk oru room venam", "manglish"),
                                         ("kitne log hai", "hinglish"), ("What is the checkout time?", "en")])
def test_reply_language_is_read_from_the_message(text, style):
    st: dict = {}
    ChatMixin._language_note(st, text)
    assert st["style"] == style


@pytest.mark.parametrize("neutral", ["ok", "2", "👍", "Hi"])
def test_neutral_messages_keep_the_last_language(neutral):
    st = {"style": "tanglish"}
    ChatMixin._language_note(st, neutral)
    assert st["style"] == "tanglish"
