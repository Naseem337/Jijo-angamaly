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
    general = pics("general", 8)
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
            photo_urls=pics(name[:6], 3), room_list=[f"{prio}0{i}" for i in (1, 2)]))
    clock = FixedClock(datetime(2026, 11, 19, 10, 0, tzinfo=IST))
    llm = MockLLM()
    llm.script.extend([MockLLM.text("[no reply]")] * 300)  # the worst AI: silent on every turn
    wa = FakeWA()
    bot = BookingBot(load_resort_config(), repo, wa, MockPayments(), clock, llm=lambda: llm)
    return type("L", (), dict(repo=repo, bot=bot, wa=wa, prop=p, general=general, rooms=rooms, clock=clock))()


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
    assert photos(lines) == ["general"] * 6 + ["Basic"] * 3  # general first, then ONLY the free top-priority room
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
    assert photos(transcript(w)) == ["general"] * 6 + ["Standard"] * 3


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
    assert photos(lines)[-3:] == ["2"] * 3  # the Tiny Cottage, the only free room today


def test_photo_requests_follow_priority_and_availability(live):
    w = live
    full(w, "Standard")  # Standard is full on the guest's dates
    chat(w, "Rooms", "Alleppey", "dec 12 to dec 14, 2 people")
    before = photos(transcript(w))
    assert before == ["general"] * 6 + ["Basic"] * 3
    chat(w, "standard room photos?")
    lines = transcript(w)
    assert photos(lines) == before and "BOT: Sorry, Beach View Standard Rooms at First Floor is not free on your dates." in lines
    chat(w, "other room photos")
    assert photos(transcript(w))[len(before):] == ["2"] * 3  # the next FREE room by priority: the cottage
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
    assert photos(lines) == ["general"] * 6 + ["Basic"] * 3 + ["Standard"] * 3
    chat(w, "Asha 9847012345")
    summary = next(x for x in w.wa.texts(PHONE) if x.startswith("Following are the glimpse of your enquiry"))
    assert "*1 Beach View Basic Room + 1 Beach View Standard Rooms at First Floor*" in summary
