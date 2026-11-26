"""Photos and availability (owner, 2026-11-26): no dates -> the form first, photos only if the guest insists; dates
known -> purpose see (photos, no availability check) or consider (THEIR dates checked: free -> 'also free' + label +
photos, not free -> no photos). A room is said free only when a tool returned it free this turn. FAKE LLM only."""

from __future__ import annotations

from test_agent_photos import STAY, block, checked, keys, photo_rows, pw, run  # noqa: F401  (pw is a fixture)
from test_bot import GUEST, world  # noqa: F401  (world is a fixture)

from app.resort.agent import guard
from app.resort.agent.state import Enquiry


def place_only(w):
    Enquiry(place=w.p1.id).save(w.repo, GUEST)


def test_no_dates_and_photos_asked_gives_the_form_and_no_photos(pw):  # noqa: F811
    place_only(pw)
    result, items, _ = run(pw, "send_photos", {"kind": "default", "purpose": "see"})
    assert keys(items) == [] and photo_rows(pw) == []
    assert result["facts"]["form_reason"] == "availability" and result["facts"]["form_lines"][0] == "Check in :"


def test_no_dates_and_the_guest_insists_gets_photos_and_a_short_reminder_note(pw):  # noqa: F811
    place_only(pw)
    result, items, _ = run(pw, "send_photos", {"kind": "default", "purpose": "see", "insist": True})
    assert keys(items)[:2] == ["room_label", "photos:2"] and result["facts"]["dates_missing"]
    assert "max 10 words" in result["note"] and "form_lines" not in result["facts"]


def test_dates_known_and_see_sends_photos_with_no_availability_check(pw):  # noqa: F811
    checked(pw)
    block(pw, pw.family, 3)  # the Family Suite is booked on their dates: 'see' still shows it
    result, items, _ = run(pw, "send_photos", {"kind": "room_type", "room_type": "Family Suite", "purpose": "see"})
    assert keys(items) == ["room_label", "photos:2"] and result["facts"]["availability_checked"] is False
    assert "also_free" not in keys(items) and "room_not_free_for_your_dates" not in result["facts"]


def test_dates_known_consider_and_booked_is_not_free_with_no_photos(pw):  # noqa: F811
    checked(pw)
    block(pw, pw.family, 3)
    result, items, _ = run(pw, "send_photos", {"kind": "room_type", "room_type": "Family Suite", "purpose": "consider"})
    assert keys(items) == [] and result["facts"]["room_not_free_for_your_dates"] == "Family Suite"


def test_dates_known_consider_and_free_says_also_free_then_label_and_photos(pw):  # noqa: F811
    checked(pw)
    result, items, _ = run(pw, "send_photos", {"kind": "room_type", "room_type": "Family Suite", "purpose": "consider"})
    assert keys(items) == ["also_free", "room_label", "photos:2"]
    assert items[0][1] == "Yes, Family Suite is also free for your dates."


def test_a_free_claim_needs_a_free_room_from_a_tool_this_turn():
    no = guard.Sources(code="", guest="")
    assert guard.problems("Yes, the Family Suite is also free for your dates.", no)
    assert guard.problems("Sorry, the Family Suite is not free on your dates.", no) == []
    assert guard.problems("Free parking is available.", no) == []  # not a room claim
    yes = guard.Sources(code="", guest="", free_now=True)
    assert guard.problems("Yes, the Family Suite is also free for your dates.", yes) == []
