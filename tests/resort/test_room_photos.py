"""Room photos follow the owner's priority AND the guest's dates (owner, 2026-11-19): 'room photos' gets the free
top-priority room's photos; a named room type gets its own photos only if it is free; 'other room' gets the next free
room type by priority. Done in code, so a silent AI cannot drop it."""

from __future__ import annotations

import pytest

from app.resort.models import RoomType
from test_bot import GUEST, reply, say, tool, world  # noqa: F401  (fixture)


@pytest.fixture()
def checked(world, tmp_path, monkeypatch):
    """Rooms checked for 12-14 Dec, 2 persons; then each room type gets its own photos."""
    from app.resort import photos as ph
    monkeypatch.setenv("RESORT_DB_PATH", str(tmp_path / "r.sqlite3"))
    monkeypatch.setattr(ph, "_shrink", lambda data: None)
    w = world
    say(w, "Alleppey 12 dec to 14 dec 2 people", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-12-12", check_out="2026-12-14", adults=2),
        reply("[no reply]"))
    assert "Yes, rooms are available for your dates." in w.wa.texts()
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 50
    w.refs = {}
    for name in ("deluxe", "family"):
        rt = w.repo.get_room_type(getattr(w, name).id)
        w.refs[name] = [ph.save_photo(png + name.encode() + bytes([i])) for i in range(2)]
        w.repo.save_room_type(RoomType(**{**rt.__dict__, "photo_urls": w.refs[name]}))
    w.wa.sent.clear()
    return w


def sent_photos(w):
    return [m.text[len("[photo] "):] for m in w.repo.list_chat(GUEST, limit=200) if m.text.startswith("[photo] ")]


def full(w, name):
    rt = w.repo.get_room_type(getattr(w, name).id)
    w.repo.save_room_type(RoomType(**{**rt.__dict__, "units": 0}))


@pytest.mark.parametrize("text", ["room photos", "photos venam", "room pics please", "rooms photo kanikkamo"])
def test_room_photos_are_the_free_top_priority_rooms_then_the_booking_goes_on(checked, text):
    w = checked
    assert say(w, text) == "photos"  # in code, the AI is not needed
    assert sent_photos(w) == w.refs["deluxe"]
    label, ask = w.wa.texts()
    assert label == "*Deluxe*"  # which room the photos are (owner, 2026-11-20)
    assert "name" in ask and "mobile" in ask  # back to the booking (in the guest's language)


def test_the_owners_priority_decides_whose_photos_go(checked):
    w = checked
    rt = w.repo.get_room_type(w.deluxe.id)
    w.repo.save_room_type(RoomType(**{**rt.__dict__, "priority": 2}))  # the owner puts Family Suite first
    say(w, "room photos")
    assert sent_photos(w) == w.refs["family"]


def test_when_the_top_room_is_full_the_next_free_rooms_photos_go(checked):
    w = checked
    full(w, "deluxe")
    say(w, "room photos")
    assert sent_photos(w) == w.refs["family"]


@pytest.mark.parametrize("text", ["family suite photos", "family room pics undo?", "suite photo kittumo family"])
def test_a_named_free_room_type_gets_its_own_photos(checked, text):
    w = checked
    say(w, text)
    assert sent_photos(w) == w.refs["family"]


def test_a_named_room_type_that_is_not_free_is_said_plainly(checked):
    w = checked
    full(w, "family")
    assert say(w, "family suite photos") == "photos"
    assert sent_photos(w) == []
    assert w.wa.texts() == ["Sorry, Family Suite is not free on your dates."]


@pytest.mark.parametrize("text", ["other room photos", "another room pics", "vere room photos undo", "dusra room ka photo"])
def test_other_room_photos_are_the_next_free_room_by_priority(checked, text):
    w = checked
    say(w, "room photos")
    say(w, text)
    assert sent_photos(w) == w.refs["deluxe"] + w.refs["family"]


def test_other_room_when_no_other_room_is_free(checked):
    w = checked
    full(w, "family")
    say(w, "other room photos")
    assert sent_photos(w) == [] and w.wa.texts() == ["Sorry, no other room is free on your dates."]


def test_the_ai_photo_tool_follows_the_same_rule(checked):
    w = checked
    full(w, "family")
    say(w, "can I see the family suite?", GUEST, tool("send_photos", place="Alleppey", room_type="Family Suite"), reply("[no reply]"))
    assert sent_photos(w) == [] and w.wa.texts()[-1] == "Sorry, Family Suite is not free on your dates."
