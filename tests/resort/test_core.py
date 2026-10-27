from datetime import date, datetime, timedelta, timezone

import pytest

from app.resort.availability import quote, room_options
from app.resort.db import ResortRepo, SoldOut
from app.resort.models import Booking, Property, RateRule, RoomType

NOW = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)


@pytest.fixture()
def repo(tmp_path):
    r = ResortRepo(tmp_path / "r.sqlite3")
    p = r.save_property(Property("Pozhiyoram Resort", advance_pct=50))
    r.save_room_type(RoomType(p.id, "Deluxe", units=2, max_adults=2, max_children=1, max_occupancy=3, base_rate=4000,
                              weekend_rate=5000))
    return r


def test_capacity_never_offers_ten_in_one_room():
    rt = RoomType("p", "Deluxe", units=5, max_adults=2, max_children=1, max_occupancy=3)
    assert room_options(rt, 10, 0, 5) == [5]
    assert room_options(rt, 2, 1, 5) == [1, 2]
    assert room_options(rt, 4, 2, 2) == [2]
    assert room_options(rt, 4, 0, 1) == []  # needs 2 rooms, only 1 free


def test_no_children_room():
    rt = RoomType("p", "Cottage", max_children=0, max_occupancy=2)
    assert room_options(rt, 2, 1, 3) == []


def test_quote_weekend_and_season(repo):
    p = repo.list_properties()[0]
    rt = repo.list_room_types(p.id)[0]
    q = quote(p, rt, [], date(2026, 11, 6), date(2026, 11, 8), 1)  # Fri + Sat nights
    assert q.total == 10000 and q.advance == 5000
    rule = RateRule(rt.id, date(2026, 11, 6), date(2026, 11, 6), 9000)
    assert quote(p, rt, [rule], date(2026, 11, 6), date(2026, 11, 8), 2).total == 28000


def test_no_double_booking_and_hold_expiry(repo):
    p = repo.list_properties()[0]
    rt = repo.list_room_types(p.id)[0]
    def mk(rooms):
        return Booking(p.id, rt.id, "+919847012345", date(2026, 11, 10), date(2026, 11, 12), 2, 0, rooms, 8000, 4000,
                       NOW, hold_expires_at=NOW + timedelta(minutes=15))
    repo.create_hold(mk(2), NOW)
    with pytest.raises(SoldOut):
        repo.create_hold(mk(1), NOW)
    later = NOW + timedelta(minutes=16)
    assert repo.free_rooms(rt, date(2026, 11, 10), date(2026, 11, 12), later) == 2
    assert len(repo.expire_holds(later)) == 1
    repo.create_hold(mk(1), later)
