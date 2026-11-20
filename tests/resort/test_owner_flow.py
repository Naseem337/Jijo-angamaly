"""The owner's whole flow with the LIVE room setup (Pozhiyoram: Basic -> Standard -> Tiny Cottage by priority, general
photos, Kanthalloor switched off) and the WORST AI (always silent). Owner, 2026-11-19: "please do what I said at all
times". Run with -s to print each transcript."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.clock import IST, FixedClock
from app.providers.llm import MockLLM
from app.resort import photos as ph
from app.resort.config import load_resort_config
from app.resort.db import ResortRepo
from app.resort.flow import BookingBot
from app.resort.models import Booking, Property, RoomType
from app.resort.payments import MockPayments
from test_bot import STAFF, FakeWA, msg

PHONE = "+918136931505"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 50


@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("RESORT_DB_PATH", str(tmp_path / "r.sqlite3"))
    monkeypatch.setattr(ph, "_shrink", lambda data: None)
    repo = ResortRepo(tmp_path / "r.sqlite3")
    pics = lambda tag, n: [ph.save_photo(PNG + tag.encode() + bytes([i])) for i in range(n)]  # noqa: E731
    general = pics("general", 12)
    p = repo.save_property(Property(
        "Pozhiyoram Resort", city="Alleppey", place="Pozhiyoram, Alleppey", advance_pct=50, staff_numbers=[STAFF],
        contact_numbers=["+919497861924"], photo_urls=general, meal_plan="Stay Only",
        intro="Your Beachfront Hideout Awaits!\n{room} - {price}/Night/Room",
        policies="*Cancellation Policy*", stay_info="Location and Wi-Fi"))
    repo.save_property(Property("Vibes Kanthalloor", city="Kanthalloor, Munnar", place="Kanthalloor", active=False))
    rooms = {}
    for name, prio, adults, rate in (("Beach View Basic Room", 1, 2, 3000),
                                     ("Beach View Standard Rooms at First Floor", 2, 3, 3000),
                                     ("2 Bedroom Tiny Kerala Cottage", 3, 3, 4000)):
        rooms[name.split()[0 if prio == 3 else 2]] = repo.save_room_type(RoomType(
            p.id, name, units=2, max_adults=adults, max_children=1, max_occupancy=3, base_rate=rate, priority=prio,
            photo_urls=pics(name, 9), room_list=[f"{prio}0{i}" for i in (1, 2)]))
    clock = FixedClock(datetime(2026, 11, 19, 10, 0, tzinfo=IST))
    llm = MockLLM()
    llm.script.extend([MockLLM.text("[no reply]")] * 300)  # the worst AI: silent on every turn
    wa = FakeWA()
    bot = BookingBot(load_resort_config(), repo, wa, MockPayments(), clock, llm=lambda: llm)
    return type("L", (), dict(repo=repo, bot=bot, wa=wa, prop=p, general=general, rooms=rooms, clock=clock, llm=llm))()


def chat(w, *texts):
    for text in texts:
        w.bot.handle(msg(PHONE, text))


def transcript(w) -> list[str]:
    """What the guest saw: texts, and each photo as 'PHOTO:<general|Basic|Standard|2>'."""
    owner = {r: "general" for r in w.general}
    for key, rt in w.rooms.items():
        owner.update({r: key for r in w.repo.get_room_type(rt.id).photo_urls})
    out = []
    for m in w.repo.list_chat(PHONE, limit=500):
        if m.direction == "out" and m.text.startswith("[photo] "):
            out.append("PHOTO:" + owner.get(m.text[8:], "?"))
        elif m.text != "[restart]":
            out.append(("GUEST: " if m.direction == "in" else "BOT: ") + m.text.split("\n")[0])
    print("\n".join(out))
    return out


def full(w, key, ci="2026-12-12", co="2026-12-14", rooms=2):
    rt = w.rooms[key]
    b = Booking(w.prop.id, rt.id, "+919000000009", date.fromisoformat(ci), date.fromisoformat(co), 2, 0, rooms,
                6000, 3000, w.clock.now(), status="paid")
    w.repo.create_hold(b, w.clock.now())


def photos(lines):
    return [x[6:] for x in lines if x.startswith("PHOTO:")]


def test_the_whole_flow_exactly_as_the_owner_said(live):
    w = live
    chat(w, "Hi", "Rooms", "Allepey", "dec 12 to dec 14, 2 people")
    lines = transcript(w)
    bot = [x for x in lines if x.startswith("BOT")]
    assert bot[:5] == ["BOT: Hi, How may I assist you?", "BOT: Sure, Kindly confirm the Location 📍",
                       "BOT: Please share these details:", "BOT: Yes, rooms are available for your dates.",
                       "BOT: Your Beachfront Hideout Awaits!"]
    assert photos(lines) == ["general"] * 12 + ["Basic"] * 9  # ALL general, then ONLY the free top-priority room
    assert lines[-1] == "BOT: Please share your name and mobile number to proceed."
    chat(w, "Naseem", "8136931505")
    texts = w.wa.texts(PHONE)
    summary = next(x for x in texts if x.startswith("Following are the glimpse of your enquiry"))
    assert "Guest - Naseem" in summary and "*Beach View Basic Room*" in summary and "Pay - 3,000" in summary
    assert texts[-1].startswith("Please pay the advance using this link:")
    assert not any("RB-" in x for x in texts)  # no booking id to the guest
    b = w.repo.list_bookings(phone=PHONE)[0]
    w.bot.mark_paid(booking_id=b.id)
    texts = w.wa.texts(PHONE)
    assert texts[-3].startswith("Following are the glimpse of your booking") and "*Booking Confirmed ✅*" in texts[-3]
    assert texts[-2:] == ["*Cancellation Policy*", "Location and Wi-Fi"]
    assert len(texts) == len(set(texts))  # nothing twice


def test_basic_full_only_standard_photos(live):
    w = live
    full(w, "Basic")
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 2 people")
    assert photos(transcript(w)) == ["general"] * 12 + ["Standard"] * 9


def test_basic_and_standard_full_only_cottage_photos(live):
    w = live
    full(w, "Basic")
    full(w, "Standard")
    chat(w, "Rooms", "Alleppey", "today 1 night 2 people")  # 'today' is read; dec 12 rooms are not in the way
    lines = transcript(w)
    assert "BOT: Yes, rooms are available for your dates." in lines
    full(w, "Basic", "2026-11-19", "2026-11-20")
    full(w, "Standard", "2026-11-19", "2026-11-20")
    chat(w, "restart", "Rooms", "Alleppey", "today 1 night 2 people")
    lines = transcript(w)
    assert photos(lines)[-9:] == ["2"] * 9  # the Tiny Cottage, the only free room today


def test_photo_requests_follow_priority_and_availability(live):
    w = live
    full(w, "Standard")  # Standard is full on the guest's dates
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 2 people")
    before = photos(transcript(w))
    assert before == ["general"] * 12 + ["Basic"] * 9
    chat(w, "standard room photos?")
    lines = transcript(w)
    assert photos(lines) == before and "BOT: Sorry, Beach View Standard Rooms at First Floor is not free on your dates." in lines
    chat(w, "other room photos")
    assert photos(transcript(w))[len(before):] == ["2"] * 9  # the next FREE room by priority: the cottage
    chat(w, "vere room photos undo")
    assert transcript(w)[-1] == "BOT: Sorry, ningalude dates-il vere room free alla." or \
        "no other room" in transcript(w)[-1].lower()


def test_kanthalloor_is_currently_not_available(live):
    w = live
    chat(w, "Rooms", "Kanthalloor")
    assert transcript(w)[-1] == "BOT: Sorry, Kanthalloor is currently not available."


def test_a_group_of_four_with_one_basic_left_gets_basic_plus_standard(live):
    w = live
    full(w, "Basic", rooms=1)
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 4 people, 2 rooms")
    lines = transcript(w)
    assert photos(lines) == ["general"] * 12 + ["Basic"] * 9 + ["Standard"] * 9
    chat(w, "Asha 9847012345")
    summary = next(x for x in w.wa.texts(PHONE) if x.startswith("Following are the glimpse of your enquiry"))
    assert "*1 Beach View Basic Room + 1 Beach View Standard Rooms at First Floor*" in summary


def test_the_live_chat_other_and_available_rooms(live):
    """Live chat 2026-11-19: 'any other rooms so that I can see multiple rooms?' got 'Which one would you like?', and
    'send the photos of rooms that are available' got more Basic photos mixed with others."""
    w = live
    chat(w, "Hi bro")
    assert transcript(w)[-1] == "BOT: Hi, How may I assist you?"
    chat(w, "Rooms", "Alleppey", "Jan 14 to 17, 2 people")
    first = photos(transcript(w))
    assert first == ["general"] * 12 + ["Basic"] * 9  # ALL general photos, then ALL of the offered room's
    chat(w, "Is there any other rooms so that I can see multiple rooms?")
    lines = transcript(w)
    assert photos(lines)[len(first):] == ["Standard"] * 9 + ["2"] * 9  # every other FREE room, never general again
    assert not any("Which one" in x for x in lines)
    assert lines[-1] == "BOT: Please share your name and mobile number to proceed."
    chat(w, "please send the photos of rooms that are available")
    assert transcript(w)[-1] == "BOT: Sorry, no other room is free on your dates."  # all were shown: said plainly
    captions = [p for _, k, p in w.wa.to(PHONE) if k == "image"]
    assert len(captions) == len(photos(transcript(w)))


def test_other_room_one_at_a_time_then_resort_photos(live):
    w = live
    chat(w, "Rooms", "Alleppey", "Jan 14 to 17, 2 people")
    n = len(photos(transcript(w)))
    chat(w, "other room photos")
    assert photos(transcript(w))[n:] == ["Standard"] * 9  # one room: the next free by priority
    chat(w, "another room pics")
    assert photos(transcript(w))[n + 9:] == ["2"] * 9
    chat(w, "beach photos?")
    assert transcript(w)[-1] == "BOT: All the photos are sent above."  # general photos were all sent already



def test_every_uploaded_photo_is_sent_no_limit(live):
    """Client, 2026-11-19: no fixed number. 30 general and 25 room photos uploaded -> all 30, then all 25."""
    w = live
    more = [ph.save_photo(PNG + b"gen-extra" + bytes([i])) for i in range(18)]
    w.repo.save_property(Property(**{**w.repo.get_property(w.prop.id).__dict__, "photo_urls": w.general + more}))
    w.general = w.general + more
    rt = w.repo.get_room_type(w.rooms["Basic"].id)
    extra = [ph.save_photo(PNG + b"basic-extra" + bytes([i])) for i in range(16)]
    w.repo.save_room_type(RoomType(**{**rt.__dict__, "photo_urls": rt.photo_urls + extra}))
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 2 people")
    assert photos(transcript(w)) == ["general"] * 30 + ["Basic"] * 25


def _logged(w):
    return [m.text[len("[photo] "):] for m in w.repo.list_chat(PHONE, limit=500) if m.text.startswith("[photo] ")]


def test_each_room_number_has_its_own_photos_and_the_guest_gets_the_rooms_they_will_get(live):
    """Owner, 2026-11-19: photos per room number; the bot sends the photos of the exact room it will give."""
    w = live
    rt = w.repo.get_room_type(w.rooms["Basic"].id)  # top priority, rooms '101', '102'
    own = {n: [ph.save_photo(PNG + b"room" + n.encode() + bytes([i])) for i in range(2)] for n in rt.room_list}
    w.repo.save_room_type(RoomType(**{**rt.__dict__, "number_photos": own}))
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 2 people")
    assert _logged(w) == w.general + own["101"]  # room 101's own photos: not 102's, not the room type's
    chat(w, "room photos")
    assert not set(own["102"]) & set(_logged(w))  # never room 102's photos: this guest would not get 102
    b = Booking(w.prop.id, rt.id, "+919000000009", date(2026, 12, 12), date(2026, 12, 14), 2, 0, 1, 6000, 3000,
                w.clock.now(), status="paid")
    w.repo.create_hold(b, w.clock.now())  # 101 is now taken on those dates
    assert w.repo.get_booking(b.id).room_numbers == "101"
    chat(w, "restart", "Rooms", "Alleppey", "dec 12 to dec 14, 2 people")
    assert _logged(w)[-2:] == own["102"]  # the next guest gets room 102, so 102's photos


def test_a_room_without_its_own_photos_falls_back_to_the_room_type_photos(live):
    w = live
    rt = w.repo.get_room_type(w.rooms["Basic"].id)
    own = [ph.save_photo(PNG + b"only102" + bytes([i])) for i in range(2)]
    w.repo.save_room_type(RoomType(**{**rt.__dict__, "number_photos": {"102": own}}))
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 2 people")
    assert _logged(w) == w.general + rt.photo_urls  # 101 has none of its own: the room type's photos


def test_old_room_type_photos_move_to_each_room_number(tmp_path):
    """Owner, 2026-11-19: no photos on the room type; its old photos are copied to each room number, nothing lost."""
    repo = ResortRepo(tmp_path / "m.sqlite3")
    rt = repo.save_room_type(RoomType("P", "Standard", units=2, photo_urls=["a.jpg", "b.jpg"], room_list=["108", "109"],
                                      number_photos={"109": ["own.jpg"]}))
    moved = ResortRepo(tmp_path / "m.sqlite3").get_room_type(rt.id)  # the move runs when the app starts
    assert moved.photo_urls == [] and moved.number_photos == {"108": ["a.jpg", "b.jpg"], "109": ["own.jpg"]}


def _live_setup(w):
    """Today's live dashboard: Standard 1 (108, 109), Tiny Cottage 2 (103, 104), Basic 3 (106, 107)."""
    for key, prio, nums in (("Standard", 1, ["108", "109"]), ("2", 2, ["103", "104"]), ("Basic", 3, ["106", "107"])):
        rt = w.repo.get_room_type(w.rooms[key].id)
        w.repo.save_room_type(RoomType(**{**rt.__dict__, "priority": prio, "room_list": nums, "max_adults": 3}))


def test_standard_full_a_group_needing_4_rooms_gets_cottage_and_basic(live):
    """Owner, 2026-11-19: both Standard rooms booked, a client needs 4 rooms -> the remaining rooms: 2 Tiny Cottage
    + 2 Basic (by priority), one booking flow, and the room numbers 103, 104, 106, 107 on payment."""
    w = live
    _live_setup(w)
    full(w, "Standard")  # 108 and 109 are booked on 12-14 Dec
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 8 people, 4 rooms")
    bk = w.repo.get_state(PHONE)["bk"]
    names = {rt.id: key for key, rt in w.rooms.items()}
    assert [(names[p["room_type_id"]], p["rooms"]) for p in bk["mix"]] == [("2", 2), ("Basic", 2)]
    assert "BOT: Yes, rooms are available for your dates." in transcript(w)
    chat(w, "Asha 9847012345")
    summary = next(x for x in w.wa.texts(PHONE) if x.startswith("Following are the glimpse of your enquiry"))
    assert "4 Rooms" in summary and "*2 2 Bedroom Tiny Kerala Cottage + 2 Beach View Basic Room*" in summary
    parts = w.repo.list_bookings(phone=PHONE)
    assert w.bot.mark_paid(booking_id=parts[0].id) == "paid"
    numbers = sorted(n for b in w.repo.list_bookings(phone=PHONE) for n in b.room_numbers.split(", "))
    assert numbers == ["103", "104", "106", "107"]
    confirmation = next(x for x in w.wa.texts(PHONE) if "Booking Confirmed" in x)
    assert "#103" in confirmation and "#107" in confirmation


def test_standard_full_8_people_no_room_count_gets_the_fewest_remaining_rooms(live):
    w = live
    _live_setup(w)
    full(w, "Standard")
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 8 people")
    bk = w.repo.get_state(PHONE)["bk"]
    names = {rt.id: key for key, rt in w.rooms.items()}
    assert [(names[p["room_type_id"]], p["rooms"]) for p in bk["mix"]] == [("2", 2), ("Basic", 1)]  # 3 rooms hold 8


def _plan(w):
    names = {rt.id: key for key, rt in w.rooms.items()}
    bk = w.repo.get_state(PHONE)["bk"]
    if bk.get("mix"):
        return [(names[p["room_type_id"]], p["rooms"], p.get("beds", 0)) for p in bk["mix"]]
    return None


def test_8_people_get_4_rooms_at_2_per_room(live):
    """Owner, 2026-11-19: normally 2 persons per room. 8 people -> 4 rooms (Standard has 2 -> + 2 Tiny Cottage)."""
    w = live
    w.repo.save_property(Property(**{**w.repo.get_property(w.prop.id).__dict__, "extra_bed_rate": 500}))
    _live_setup(w)
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 8 people")
    assert _plan(w) == [("Standard", 2, 0), ("2", 2, 0)]
    # no extra beds unless the guest asks fewer rooms; the note before the photos says the other way (owner 2026-11-20)
    assert ("For 8 persons, 4 rooms are needed. Or 8 persons can stay in 3 room(s) with extra beds (₹500 per extra "
            "bed per night).") in w.wa.texts(PHONE)
    assert not any("extra bed(s) added" in x for x in w.wa.texts(PHONE))


def test_fewer_rooms_only_when_asked_with_extra_beds_at_500(live):
    w = live
    w.repo.save_property(Property(**{**w.repo.get_property(w.prop.id).__dict__, "extra_bed_rate": 500}))
    _live_setup(w)
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 8 people, 3 rooms")
    assert _plan(w) == [("Standard", 2, 2), ("2", 1, 0)]  # 2 Standard with an extra bed each + 1 Tiny Cottage
    texts = w.wa.texts(PHONE)
    assert "2 extra bed(s) added: ₹500 per bed per night." in texts
    assert texts.index("Yes, rooms are available for your dates.") < texts.index("2 extra bed(s) added: ₹500 per bed per night.")
    chat(w, "Asha 9847012345")
    summary = next(x for x in w.wa.texts(PHONE) if x.startswith("Following are the glimpse of your enquiry"))
    assert "Extra Bed - 2,000" in summary and "3 Rooms" in summary  # 2 beds x 500 x 2 nights


def test_one_room_type_fewer_rooms_asked(live):
    w = live
    w.repo.save_property(Property(**{**w.repo.get_property(w.prop.id).__dict__, "extra_bed_rate": 500}))
    _live_setup(w)
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 3 people")
    assert w.repo.get_state(PHONE)["bk"]["rooms"] == 0 and _plan(w) is None
    assert "Please share your name and mobile number to proceed." in w.wa.texts(PHONE)
    texts_before = len(w.wa.texts(PHONE))
    assert any(x.startswith("For 3 persons, 2 rooms are needed. Or 3 persons can stay in 1 room(s)")
               for x in w.wa.texts(PHONE))  # 3 people -> 2 rooms, and the extra-bed way is offered before photos


def test_too_few_rooms_asked_says_the_minimum(live):
    w = live
    w.repo.save_property(Property(**{**w.repo.get_property(w.prop.id).__dict__, "extra_bed_rate": 500}))
    _live_setup(w)
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 8 people, 2 rooms")
    texts = w.wa.texts(PHONE)
    # never a dead end (live chat 2026-11-20): the minimum is said, and 3 rooms with extra beds are checked at once
    assert "Max 3 persons per room (2 + 1 extra bed), so 8 persons need at least 3 rooms." in texts
    assert "Yes, rooms are available for your dates." in texts
    assert texts[-1] == "Please share your name and mobile number to proceed."
    assert not any("not available" in x for x in texts)
    assert _plan(w) == [("Standard", 2, 2), ("2", 1, 0)]
    chat(w, "Ok")  # 'Ok' never brings back the location question
    assert not any("Kindly confirm the Location" in x for x in w.wa.texts(PHONE)[len(texts):])


def test_form_with_bare_checkout_day_and_ok_never_asks_location_again(live):
    w = live
    w.repo.save_property(Property(**{**w.repo.get_property(w.prop.id).__dict__, "extra_bed_rate": 500}))
    _live_setup(w)
    chat(w, "Room indo avde", "Alleppey")
    n = len(w.wa.texts(PHONE))
    chat(w, "Please share these details:\n\nCheck in :dec 29\nCheck out :30\nNumber of persons :3\nNumber of rooms:1")
    new = w.wa.texts(PHONE)[n:]
    assert not any(x.startswith("Please share these details") for x in new)  # 'Check out :30' = 30 Dec
    assert any("rooms are available" in x.lower() or "rooms available" in x.lower() for x in new)
    m = len(w.wa.texts(PHONE))
    chat(w, "Ok")
    assert not any("Kindly confirm the Location" in x for x in w.wa.texts(PHONE)[m:])


def test_an_old_unpaid_enquiry_never_gets_a_reminder_after_a_new_booking(live):
    """Live chat 2026-11-19: Shibil left a 23-25 Dec enquiry unpaid, booked and paid 25-27 Dec, then got 'Your payment
    link is still open for 4 more minutes'. A new booking closes the old enquiry and its link."""
    from datetime import timedelta
    w = live
    _live_setup(w)
    chat(w, "Rooms", "Alleppey", "dec 23 to dec 25, 1 person")
    chat(w, "Shibil 8590629268")
    old = w.repo.list_bookings(phone=PHONE)[0]
    assert old.status == "hold"
    chat(w, "restart", "Rooms", "Alleppey", "dec 25 to dec 27, 2 people")
    chat(w, "Shibil 8590629268")
    new = next(b for b in w.repo.list_bookings(phone=PHONE) if b.id != old.id)
    assert w.repo.get_booking(old.id).status == "expired"  # closed when the new enquiry was made
    assert old.payment_link_id in w.bot.payments.cancelled  # its link can no longer be paid
    w.bot.mark_paid(booking_id=new.id)
    sent = len(w.wa.texts(PHONE))
    w.clock.advance(minutes=w.bot.config.hold_minutes - 3)  # the old hold would have been 'about to expire'
    w.bot.sweep()
    assert w.wa.texts(PHONE)[sent:] == []  # no 'still open' message after the confirmation


def test_a_big_group_not_fitting_2_per_room_is_offered_extra_beds_then_ok_books(live):
    """Live chat 2026-11-19: 15 people on 25-27 Dec got only 'not available'. 6 rooms x 2 = 12 < 15, but 5 rooms with
    extra beds fit: the bot offers that, and 'ok' goes ahead."""
    w = live
    w.repo.save_property(Property(**{**w.repo.get_property(w.prop.id).__dict__, "extra_bed_rate": 500}))
    _live_setup(w)
    chat(w, "Rooms", "Alleppey", "Check in :dec 25\nCheck out :dec 27\nNumber of persons :15")
    assert transcript(w)[-1] == ("BOT: 15 persons fit in 5 rooms with extra beds (2 + 1 per room, ₹500 per extra bed "
                                 "per night). Shall I go ahead with 5 rooms?")
    chat(w, "ok")
    lines = transcript(w)
    assert "BOT: Yes, rooms are available for your dates." in lines
    assert "BOT: 5 extra bed(s) added: ₹500 per bed per night." in lines


def test_not_available_suggests_the_nearest_free_dates(live):
    w = live
    _live_setup(w)
    for key in ("Standard", "2", "Basic"):
        full(w, key, "2026-12-12", "2026-12-14")  # everything is booked on 12-14 Dec
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 2 people")
    assert transcript(w)[-1] == "BOT: Sorry, rooms are not available for your dates. Rooms are free on 10 Dec - 12 Dec or 14 Dec - 16 Dec."
    chat(w, "14 dec to 16 dec")
    assert "BOT: Yes, rooms are available for your dates." in transcript(w)  # the persons are kept


def test_a_lone_date_after_check_out_is_asked_is_the_check_out_and_nudges_get_a_reply(live):
    """Live chat 2026-11-19: 'For 29th Nov.?' -> 'Check out :' -> '30th Nov' got no reply; 'Please respond' too."""
    w = live
    _live_setup(w)
    chat(w, "Rooms", "Alleppey", "3 Ppl")
    chat(w, "For 29th Nov.?")
    assert transcript(w)[-1] == "BOT: Please share these details:"
    chat(w, "30th Nov")
    assert "BOT: Yes, rooms are available for your dates." in transcript(w)
    n = len(w.wa.texts(PHONE))
    chat(w, "Please respond")
    assert len(w.wa.texts(PHONE)) > n


# ------------------------------------------------- answer like an agent (owner, 2026-11-20)
from app.providers.llm import MockLLM  # noqa: E402


def _ai_says(w, *lines):
    llm = w.bot._llm()
    llm.script[:0] = [MockLLM.text(x) for x in lines]


def test_a_question_with_a_room_request_is_answered_then_the_location_is_asked(live):
    w = live
    _ai_says(w, "Yes, every room has a sea view.")
    chat(w, "Do you have rooms with a sea view?")
    lines = transcript(w)
    assert lines[-2:] == ["BOT: Yes, every room has a sea view.", "BOT: Sure, Kindly confirm the Location 📍"]


def test_details_and_a_question_in_one_message_both_get_an_answer(live):
    w = live
    _live_setup(w)
    chat(w, "Rooms", "Alleppey")
    _ai_says(w, "Yes, parking is free for guests.")
    chat(w, "25 dec to 27 dec, 2 people, is parking free?")
    lines = transcript(w)
    i = lines.index("BOT: Yes, parking is free for guests.")
    assert "BOT: Yes, rooms are available for your dates." in lines[i:]  # the answer first, then the room check


def test_the_ai_never_states_room_facts_beside_the_fixed_steps(live):
    w = live
    _live_setup(w)
    chat(w, "Rooms", "Alleppey")
    _ai_says(w, "Rooms are available and the room fits 3 people. The pool is open 7 am to 7 pm.")
    chat(w, "25 dec to 27 dec, 3 ppl, pool timing?")
    lines = transcript(w)
    assert "BOT: The pool is open 7 am to 7 pm." in lines
    assert not any("fits 3" in x for x in lines)


def test_plain_details_never_call_the_ai(live):
    w = live
    _live_setup(w)
    chat(w, "Rooms", "Alleppey")
    calls = len(w.bot._llm().calls)
    chat(w, "25 Dec\n3 ppl", "26 Dec")
    assert len(w.bot._llm().calls) == calls  # pure booking details are handled in code, fast and exact
    assert "BOT: Yes, rooms are available for your dates." in transcript(w)


def test_a_question_nothing_fixed_fits_gets_an_ai_answer_not_type_again(live):
    w = live
    _ai_says(w, "[no reply]", "Alleppey beach is 2 minutes walk from the resort.")
    chat(w, "how far is the beach?")
    assert transcript(w)[-1] == "BOT: Alleppey beach is 2 minutes walk from the resort."


def test_two_rooms_photos_each_start_with_the_room_number_and_name(live):
    # owner, 2026-11-20: between two rooms' photos a text says which room and number they are
    w = live
    _live_setup(w)
    rt = w.repo.get_room_type(w.rooms["Standard"].id)
    own = {n: [ph.save_photo(PNG + b"num" + n.encode() + bytes([i])) for i in range(2)] for n in rt.room_list}
    w.repo.save_room_type(RoomType(**{**rt.__dict__, "photo_urls": [], "number_photos": own}))
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 4 people")
    sent = [(k, p) for _, k, p in w.wa.to(PHONE)]
    label_108 = sent.index(("text", f"*Room 108 - {rt.name}*"))
    label_109 = sent.index(("text", f"*Room 109 - {rt.name}*"))
    assert [k for k, _ in sent[label_108 + 1:label_109]] == ["image", "image"]  # 108's photos between the labels
    assert [k for k, _ in sent[label_109 + 1:label_109 + 3]] == ["image", "image"]


def test_yes_to_the_bots_own_question_is_never_silent(live):
    # live chat 2026-11-20: 'Shall I book 3 rooms?' -> 'Yes' -> 'Ok' got nothing
    w = live
    _live_setup(w)
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 8 people")
    w.bot.flow._text(PHONE, "8 guests need 3 rooms. Shall I book 3 rooms?") if hasattr(w.bot, "flow") else \
        w.bot._text(PHONE, "8 guests need 3 rooms. Shall I book 3 rooms?")
    n = len(w.wa.to(PHONE))
    chat(w, "Yes")
    assert len(w.wa.to(PHONE)) > n  # something always goes back


def test_room_counts_always_come_from_the_code_never_the_ai(live):
    # live chat 2026-11-20: 6 people, then '7 ppl', then 'how many rooms' -> the AI said '2 rooms with 1 extra bed
    # in each' (only 6 fit) and 'Let me check ... One moment'. Room maths is the code's, for every number of persons.
    w = live
    w.repo.save_property(Property(**{**w.repo.get_property(w.prop.id).__dict__, "extra_bed_rate": 500}))
    _live_setup(w)
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14", "6 people")
    assert "For 6 persons, 3 rooms are needed. Or 6 persons can stay in 2 room(s) with extra beds (₹500 per extra " \
           "bed per night)." in w.wa.texts(PHONE)
    chat(w, "7 ppl")
    seven = "For 7 persons, 4 rooms are needed. Or 7 persons can stay in 3 room(s) with extra beds (₹500 per extra " \
            "bed per night)."
    assert seven in w.wa.texts(PHONE)  # the new number of persons gets the code's sentence too
    n = len(w.wa.texts(PHONE))
    bad = "For 7 people, you need 4 rooms. Or you can take 2 rooms with 1 extra bed in each. One moment."
    w.llm.script[:0] = [MockLLM.text(bad)] * 3
    chat(w, "how many rooms")
    new = w.wa.texts(PHONE)[n:]
    assert not any("2 rooms with 1 extra bed" in x or "One moment" in x for x in new)
    assert any(seven in x for x in new)  # the AI's maths is replaced by the code's sentence
