"""Owner, 2026-11-19: mixed room types for one group, 'today' and the newest date win, no made-up 'fully booked', and
'Is there anything else?' once after a final 'ok'."""

from __future__ import annotations

from datetime import date, timedelta

from app.resort.chat import MSG_SEP, parse_stay
from app.resort.models import Booking
from test_bot import GUEST, reply, say, tool, world  # noqa: F401  (fixture)

FORM = "Please share these details:\n\nCheck in :\nCheck out :\nNumber of persons :"
TODAY = date(2026, 11, 2)


def test_today_tomorrow_and_a_day_are_read():
    assert parse_stay("Today", TODAY) == {"check_in": TODAY}
    assert parse_stay("naale 2 per", TODAY) == {"check_in": TODAY + timedelta(days=1), "persons": 2}
    assert parse_stay(f"Today{MSG_SEP}I will be there for a day", TODAY) == {
        "check_in": TODAY, "check_out": TODAY + timedelta(days=1)}
    assert parse_stay("aaj ek din 3 log", TODAY)["check_out"] == TODAY + timedelta(days=1)


def test_the_newest_date_replaces_an_older_one():
    got = parse_stay(f"dec12 nn verum pinne 2 disam njmal 3 per und{MSG_SEP}No i want it for today", TODAY)
    assert got["check_in"] == TODAY and "check_out" not in got and got["persons"] == 3
    got = parse_stay(f"dec 12{MSG_SEP}3 persons, till 15", TODAY)
    assert got == {"check_in": date(2026, 12, 12), "check_out": date(2026, 12, 15), "persons": 3}


def test_the_live_chat_today_gets_a_room_check_for_today_not_old_dates(world):
    w = world
    say(w, "Rooms?")
    say(w, "Alleppey")
    say(w, "dec12 nn verum pinne 2 disam njan indavum njmal 3 per und")
    say(w, "Hi")
    say(w, "Need room", GUEST, reply("[no reply]"))
    say(w, "Today", GUEST, reply("The check-in is 12 December."))  # the AI is not asked: code reads 'today'
    assert w.wa.texts()[-1] == "Please share these details:\n\nCheck out :"  # 3 persons were said earlier
    say(w, "Iw ill bethere for a day", GUEST, reply("I can help with resort bookings only."))
    bk = w.repo.get_state(GUEST)["bk"]
    assert (bk["check_in"], bk["check_out"]) == (TODAY.isoformat(), (TODAY + timedelta(days=1)).isoformat())
    assert sum("rooms available" in x or "rooms are available" in x for x in w.wa.texts()) == 2  # Dec 12 earlier, today now
    assert not any("12 December" in x or "resort bookings only" in x for x in w.wa.texts())


def test_a_made_up_fully_booked_is_never_sent(world):
    w = world
    say(w, "Alleppey 12 dec to 14 dec 2 people")
    w.wa.sent.clear()
    say(w, "is it available today?", GUEST, reply("Sorry, today is already fully booked. Would you like another date?"))
    assert not any("fully booked" in x for x in w.wa.texts())


def _fill(w, rt, rooms, ci="2026-11-10", co="2026-11-11"):
    b = Booking(w.p1.id, rt.id, "+919000000001", date.fromisoformat(ci), date.fromisoformat(co), 2, 0, rooms, 4000,
                2000, w.clock.now(), status="paid")
    w.repo.create_hold(b, w.clock.now())


def test_four_guests_two_rooms_one_deluxe_left_gets_one_deluxe_and_one_family_suite(world):
    w = world
    _fill(w, w.deluxe, 1)  # one of the 2 Deluxe rooms is booked
    say(w, "Alleppey 10 nov 1 night, 4 people, 2 rooms", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=1, adults=4, rooms=2),
        reply("[no reply]"))
    bk = w.repo.get_state(GUEST)["bk"]
    assert [(p["room_type_id"], p["rooms"]) for p in bk["mix"]] == [(w.deluxe.id, 1), (w.family.id, 1)]
    say(w, "Asha 9847012345", GUEST, reply("[no reply]"))
    parts = sorted(w.repo.list_bookings(phone=GUEST), key=lambda b: b.room_type_id != w.deluxe.id)
    assert [(b.room_type_id, b.rooms, b.status) for b in parts] == [(w.deluxe.id, 1, "hold"), (w.family.id, 1, "hold")]
    assert parts[0].group_id == parts[1].group_id and parts[0].payment_link_id == parts[1].payment_link_id
    summary = next(x for x in w.wa.texts() if x.startswith("Following are the glimpse of your enquiry"))
    assert "*1 Deluxe + 1 Family Suite*" in summary and "2 Rooms" in summary
    assert w.bot.mark_paid(link_id=parts[1].payment_link_id) == "paid"
    assert {b.status for b in w.repo.list_bookings(phone=GUEST)} == {"paid"}
    confirmations = [x for x in w.wa.texts() if "Booking Confirmed" in x]
    assert len(confirmations) == 1 and "1 Deluxe + 1 Family Suite" in confirmations[0]
    assert w.bot.mark_paid(link_id=parts[0].payment_link_id) == "already_paid"


def test_ten_guests_still_get_the_fewest_rooms_of_one_type(world):
    w = world
    say(w, "10 of us, Alleppey 10 Nov 1 night", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=1, adults=10), reply("ok"))
    assert "mix" not in w.repo.get_state(GUEST)["bk"]


def test_a_final_ok_gets_anything_else_once_and_no_gets_nothing(world):
    w = world
    w.bot.followup_s = 0.01
    say(w, "Is there a beach view?", GUEST, reply("Yes, every room."))
    assert say(w, "ok") == "anything_else" and w.wa.texts()[-1] == "Is there anything else?"
    n = len(w.wa.to(GUEST))
    assert say(w, "no") == "quiet" and len(w.wa.to(GUEST)) == n
    assert say(w, "ok") == "quiet" and len(w.wa.to(GUEST)) == n  # once per conversation


def test_clearing_test_data_keeps_rooms_and_guests(world):
    w = world
    from test_bot import hold
    hold(w)
    assert w.repo.list_bookings(phone=GUEST)
    counts = w.repo.clear_bookings_and_chats()
    assert counts["bookings"] == 1 and counts["chat"] > 0
    assert w.repo.list_bookings() == [] and w.repo.list_chat(GUEST) == [] and w.repo.get_state(GUEST) == {}
    assert w.repo.list_room_types(w.p1.id) and w.repo.get_guest(GUEST) is not None


def test_demo_data_fills_the_dashboard_without_sending_anything(world):
    from app.resort.demo import seed_demo
    w = world
    out = seed_demo(w.repo, w.clock.now())
    assert out["bookings"] >= 10 and out["chat_messages"] > 20
    assert w.wa.sent == []
    assert {b.status for b in w.repo.list_bookings()} >= {"paid", "checked_in", "checked_out", "hold", "cancelled"}


def test_demo_data_is_removed_and_real_data_stays(world):
    from app.resort.demo import clear_demo, seed_demo
    from test_bot import hold
    w = world
    hold(w)
    seed_demo(w.repo, w.clock.now())
    out = clear_demo(w.repo)
    assert out["bookings"] >= 10
    assert [b.guest_phone for b in w.repo.list_bookings()] == [GUEST]
