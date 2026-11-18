"""The client's strict flow helpers (2026-11-18): stay details read in code, contact numbers, no unasked prices."""

from __future__ import annotations

from datetime import date

import pytest

from app.resort.chat import contact_in, no_unasked_prices, parse_stay

TODAY = date(2026, 11, 18)


@pytest.mark.parametrize("text, ci, co, persons", [
    ("Check in :dec 12\nCheck out :dec 15\nNumber of persons :3", date(2026, 12, 12), date(2026, 12, 15), 3),
    ("12 dec to 14 dec, 2 people", date(2026, 12, 12), date(2026, 12, 14), 2),
    ("dec 12\n3 persons, till 15", date(2026, 12, 12), date(2026, 12, 15), 3),
    ("25/12 2 nights 4 pax", date(2026, 12, 25), date(2026, 12, 27), 4),
    ("Check in : 2nd Jan\nCheck out : 4th Jan\nNumber of persons : 5", date(2027, 1, 2), date(2027, 1, 4), 5),
    ("30 dec - 2 jan, 2 per", date(2026, 12, 30), date(2027, 1, 2), 2),
])
def test_full_stay_details_are_read(text, ci, co, persons):
    assert parse_stay(text, TODAY) == {"check_in": ci, "check_out": co, "persons": persons}


@pytest.mark.parametrize("text, expected", [
    ("2 people", {"persons": 2}),
    ("12 dec", {"check_in": date(2026, 12, 12)}),
    ("25 Dec 2 nights for 2", {"check_in": date(2026, 12, 25), "check_out": date(2026, 12, 27)}),  # '2' alone: no guess
    ("is it near the beach?", {}),
    ("Rahul, 9876543210", {}),
])
def test_partial_details_never_guess(text, expected):
    assert parse_stay(text, TODAY) == expected


@pytest.mark.parametrize("text, expected", [
    ("same number", ("", "same")),
    ("Rahul Nair, 9876543210", ("Rahul Nair", "+919876543210")),
    ("my name is Anu and number 98470 12345", ("Anu", "+919847012345")),
    ("Asha same number", ("Asha", "same")),
    ("this number is fine", ("", "same")),
    ("same dates please", None),
    ("is this near the beach?", None),
    ("I want one more room for my friends", None),
])
def test_contact_numbers_are_found(text, expected):
    assert contact_in(text) == expected


def test_prices_are_dropped_unless_the_guest_asked():
    ai = "The total for 3 nights is 9,000 rupees. Advance now is 4,500 rupees. Please share your name."
    assert no_unasked_prices(ai, "same number") == "Please share your name."
    assert no_unasked_prices("Deluxe is Rs 4,000 a night.", "price?") == "Deluxe is Rs 4,000 a night."
    assert no_unasked_prices("Yes, right on the beach.", "near beach?") == "Yes, right on the beach."
