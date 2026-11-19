"""Real-world ways guests answer the form, run against the WORST AI (it always says [no reply]).

Owner, 2026-11-19: "dec12 nn verum pinne 2 disam njan indavum njmal 3 per und" got no reply at all. The rule tested
here: after the form, every message gets an answer, and complete details always reach the availability check.
"""

from __future__ import annotations

import pytest

from test_bot import GUEST, reply, say, world  # noqa: F401  (fixture)

FORM = "Please share these details:"

# complete details: the rooms must be checked (rooms available / not), whatever the AI does
COMPLETE = [
    "dec12 nn verum pinne 2 disam njan indavum njmal 3 per und",
    "Check in : 12 dec\nCheck out : 14 dec\nNumber of persons : 3",
    "12 dec to 14 dec 3 people",
    "dec 12 - dec 14, 3 pax",
    "12/12 to 14/12 3 persons",
    "dec 12 2 nights 3 per",
    "december 12 3 divasam 2 per",
    "dec 12 for 2 days, 4 people",
    "12th dec checkin 14th dec checkout 2 adults",
    "dec 12 nu varum 14 nu pokum 3 per",
    "dec 12 muthal 14 vare 2 per",
    "12 dec il 2 rathri 3 aal",
    "dec 12 la vandhu 2 naal irupom 4 per",
    "dec 12 se 14 tak 2 log",
    "Dec 12 to Dec 15, we are 4",
    "from 12 dec till 15, 2 people",
    "12-12-2026 to 14-12-2026, 3 persons",
    "2 people, 12 dec, 2 nights",
]

# partial or odd answers: no dates check yet, but never silence
PARTIAL = [
    "dec 12",
    "3 per",
    "next week",
    "naale",
    "this weekend 2 per",
    "christmas time",
    "not sure about dates",
    "how much for 2 nights?",
    "pool undo?",
    "ok",
    "?",
    "👍",
    "dec 12 nu varum",
    "2 adults 1 child",
    "can i come tomorrow",
    "rate ethra",
    "photos venam",
    "is it near the beach",
    "family aanu",
    "dec 31 night party undo",
]


def _to_form(w):
    say(w, "Rooms?")
    say(w, "Alleppey")
    assert w.wa.texts()[-1].startswith(FORM)


@pytest.mark.parametrize("text", COMPLETE)
def test_complete_details_always_reach_the_room_check(world, text):
    w = world
    _to_form(w)
    before = len(w.wa.texts())
    say(w, text, GUEST, reply("[no reply]"), reply("[no reply]"))  # the AI does nothing useful
    new = w.wa.texts()[before:]
    assert new, f"silence after {text!r}"
    assert any("available" in x for x in new), (text, new)  # rooms were checked in code


@pytest.mark.parametrize("text", PARTIAL)
def test_a_partial_or_odd_answer_after_the_form_is_never_silent(world, text):
    w = world
    _to_form(w)
    before = len(w.wa.to(GUEST))
    say(w, text, GUEST, reply("[no reply]"), reply("[no reply]"))
    if text in ("ok", "👍"):
        return  # 'ok' right after the form: the open step is the last thing we said, wait for their answer
    assert len(w.wa.to(GUEST)) > before, f"silence after {text!r}"


# ---------------------------------------------------------------- every other stage, with a silent AI
from test_bot import hold, tool  # noqa: E402

QUIET_OK = {"ok", "okay", "thanks", "👍", "ok thanks", "later", "will tell", "sheri"}


def _stage(w, stage):
    if stage == "greeted":
        say(w, "hi")
    elif stage == "location":
        say(w, "Rooms?")
    elif stage == "name":
        say(w, "Alleppey 12 dec to 14 dec 2 people", GUEST,
            tool("check_availability", place="Alleppey", check_in="2026-12-12", check_out="2026-12-14", adults=2),
            reply("[no reply]"))
    elif stage == "pay_link":
        hold(w)
    elif stage == "confirmed":
        hold(w)
        w.bot.mark_paid(booking_id=w.repo.list_bookings(phone=GUEST)[0].id)


MESSAGES = ["rooms?", "allepy", "kanthalloor", "Rahul 9876543210", "same number", "how much?", "pool undo?",
            "photos?", "my booking?", "cancel", "talk to owner", "?", "asdfgh", "dec 20", "3 per", "hi", "hello",
            "is it near the beach", "ok", "thanks", "👍"]


@pytest.mark.parametrize("stage", ["greeted", "location", "name", "pay_link", "confirmed"])
@pytest.mark.parametrize("text", MESSAGES)
def test_no_stage_leaves_a_real_message_without_an_answer(world, stage, text):
    w = world
    _stage(w, stage)
    before = len(w.wa.to(GUEST))
    say(w, text, GUEST, reply("[no reply]"), reply("[no reply]"), reply("[no reply]"))
    if text.lower() in QUIET_OK:
        return  # a plain 'ok' / 'thanks' may stay quiet when nothing is open
    sent = len(w.wa.to(GUEST)) > before or w.repo.is_paused(GUEST)[0]  # handed to the owner counts
    assert sent, f"silence at {stage!r} after {text!r}"


@pytest.mark.parametrize("remark", ["nice place", "will tell my family and come back", "ok we will see", "super resort"])
def test_a_remark_the_ai_leaves_quiet_stays_quiet(world, remark):
    w = world
    say(w, "Is there a beach view?", GUEST, reply("Yes, every room."))
    before = len(w.wa.to(GUEST))
    say(w, remark, GUEST, reply("[no reply]"))
    assert len(w.wa.to(GUEST)) == before  # no 'please type that once more' for a compliment or a goodbye
