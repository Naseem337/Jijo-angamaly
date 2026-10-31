import csv
import io
import re
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.resort.dashboard import integrations, router
from app.resort.dashboard.deps import BASE, get_repo, now_utc
from app.resort.dashboard.routes import today_ist
from app.resort.db import hash_password
from app.resort.models import Booking, DashboardUser, Property, RoomType

PHONE = "+919847012345"
ANNOUNCED: list[str] = []


@pytest.fixture(autouse=True)
def no_whatsapp(monkeypatch):
    """Desk payments try to message the guest; record that instead of calling WhatsApp."""
    ANNOUNCED.clear()
    monkeypatch.setattr(integrations, "announce_paid", lambda b: ANNOUNCED.append(b.id))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESORT_DB_PATH", str(tmp_path / "resort.sqlite3"))
    repo = get_repo()
    repo.save_user(DashboardUser("owner@x.com", hash_password("owner-pass-1"), "Owner", "owner"))
    p1 = repo.save_property(Property("Pozhiyoram Resort"))
    p2 = repo.save_property(Property("Vibes Kanthalloor"))
    rt1 = repo.save_room_type(RoomType(p1.id, "Deluxe", units=1, base_rate=4000))
    rt2 = repo.save_room_type(RoomType(p2.id, "Cottage", units=2, base_rate=6000))
    repo.save_user(DashboardUser("mgr@x.com", hash_password("manager-pass-1"), "Mgr", "manager", [p1.id]))
    app = FastAPI()
    app.include_router(router)
    return repo, app, (p1, p2, rt1, rt2)


def login(app, email="owner@x.com", password="owner-pass-1"):
    client = TestClient(app)
    resp = client.post(f"{BASE}/login", data={"email": email, "password": password})
    assert resp.status_code == 200
    token = re.search(r'name="csrf" value="([^"]+)"', resp.text).group(1)
    return client, token


def booking(props, rt, day=10, rooms=1, status="paid"):
    now = now_utc()
    d = today_ist() + timedelta(days=day)
    return Booking(props.id, rt.id, PHONE, d, d + timedelta(days=2), 2, 0, rooms, 8000, 4000, now, status=status,
                   guest_name="Asha", hold_expires_at=now + timedelta(minutes=15) if status == "hold" else None)


def test_login_required(env):
    _, app, _ = env
    resp = TestClient(app).get(f"{BASE}/bookings", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"{BASE}/login"
    assert TestClient(app).get(f"{BASE}/login").status_code == 200


def test_wrong_password_rejected(env):
    _, app, _ = env
    client = TestClient(app)
    resp = client.post(f"{BASE}/login", data={"email": "owner@x.com", "password": "nope"})
    assert resp.status_code == 401 and "Wrong email or password" in resp.text
    assert client.get(f"{BASE}/bookings", follow_redirects=False).status_code == 303


def test_pages_render(env):
    repo, app, (p1, p2, rt1, rt2) = env
    repo.create_hold(booking(p1, rt1, status="hold"), now_utc())
    client, _ = login(app)
    for path in ["/", "/bookings", "/bookings/new", "/calendar", "/properties", f"/properties/{p1.id}",
                 f"/properties/{p1.id}/rooms/{rt1.id}", "/properties/new", "/guests", "/conversations", "/users",
                 "/settings", "/search?q=asha", "/bookings/export.csv"]:
        assert client.get(BASE + path).status_code == 200, path


def test_create_property_and_room_type(env):
    repo, app, _ = env
    client, token = login(app)
    resp = client.post(f"{BASE}/properties/new", data={
        "csrf": token, "name": "Sea View Staycation", "city": "Varkala", "advance_pct": "50", "active": "on",
        "staff_numbers": "9847012345", "photo_urls": "https://x/a.jpg\nhttps://x/b.jpg"})
    assert resp.status_code == 200
    prop = next(p for p in repo.list_properties() if p.name == "Sea View Staycation")
    assert prop.staff_numbers == [PHONE] and len(prop.photo_urls) == 2
    client.post(f"{BASE}/properties/{prop.id}/rooms/new", data={
        "csrf": token, "name": "Villa", "units": "3", "max_adults": "4", "max_children": "2", "max_occupancy": "6",
        "base_rate": "9000", "weekend_rate": "11000", "active": "on"})
    rt = repo.list_room_types(prop.id)[0]
    assert (rt.name, rt.units, rt.base_rate, rt.weekend_rate) == ("Villa", 3, 9000, 11000)


def test_manager_sees_only_own_property(env):
    repo, app, (p1, p2, rt1, rt2) = env
    own, other = booking(p1, rt1, 5), booking(p2, rt2, 6)
    repo.create_hold(own, now_utc())
    repo.create_hold(other, now_utc())
    client, _ = login(app, "mgr@x.com", "manager-pass-1")
    page = client.get(f"{BASE}/bookings").text
    assert own.id in page and other.id not in page
    assert client.get(f"{BASE}/bookings/{other.id}").status_code == 404
    assert client.get(f"{BASE}/properties/{p2.id}").status_code == 404
    assert client.get(f"{BASE}/users", follow_redirects=False).status_code == 403
    assert other.id not in client.get(f"{BASE}/bookings/export.csv").text


def test_manual_booking_blocks_rooms(env):
    repo, app, (p1, p2, rt1, rt2) = env
    client, token = login(app)
    d = today_ist() + timedelta(days=20)
    data = {"csrf": token, "room_type_id": rt1.id, "check_in": d.isoformat(), "check_out": (d + timedelta(days=2)).isoformat(),
            "adults": "2", "children": "0", "rooms": "1", "guest_name": "Ravi", "phone": "9847012345", "payment": "paid"}
    assert client.post(f"{BASE}/bookings/new", data=data).status_code == 200
    first = repo.list_bookings(property_id=p1.id)
    assert len(first) == 1 and first[0].status == "paid" and first[0].total == 8000
    assert repo.free_rooms(rt1, d, d + timedelta(days=2), now_utc()) == 0
    resp = client.post(f"{BASE}/bookings/new", data=data, follow_redirects=False)
    assert resp.status_code == 303 and "toast=" in resp.headers["location"]
    assert len(repo.list_bookings(property_id=p1.id)) == 1
    # a party that cannot fit the requested rooms is refused too
    data["rooms"] = "1"
    data["adults"] = "10"
    assert len(repo.list_bookings(property_id=p1.id)) == 1
    client.post(f"{BASE}/bookings/new", data=data)
    assert len(repo.list_bookings(property_id=p1.id)) == 1
    quote = client.get(f"{BASE}/api/quote", params={"room_type_id": rt2.id, "check_in": d.isoformat(),
                       "check_out": (d + timedelta(days=1)).isoformat(), "adults": "2", "children": "0"}).json()
    assert quote["ok"] and quote["total"] == 6000 and quote["free"] == 2


def test_mark_paid_and_cancel(env):
    repo, app, (p1, _, rt1, _) = env
    b = booking(p1, rt1, 7, status="hold")
    repo.create_hold(b, now_utc())
    client, token = login(app)
    client.post(f"{BASE}/bookings/{b.id}/action", data={"csrf": token, "action": "mark_paid"})
    saved = repo.get_booking(b.id)
    assert saved.status == "paid" and saved.paid_at is not None and saved.hold_expires_at is None
    client.post(f"{BASE}/bookings/{b.id}/action", data={"csrf": token, "action": "note", "note": "Late arrival"})
    assert "Late arrival" in repo.get_booking(b.id).notes
    client.post(f"{BASE}/bookings/{b.id}/action", data={"csrf": token, "action": "cancel"})
    assert repo.get_booking(b.id).status == "cancelled"


def test_csrf_required(env):
    repo, app, (p1, _, rt1, _) = env
    b = booking(p1, rt1, 7, status="hold")
    repo.create_hold(b, now_utc())
    client, token = login(app)
    assert client.post(f"{BASE}/bookings/{b.id}/action", data={"action": "mark_paid"}).status_code == 403
    assert client.post(f"{BASE}/bookings/{b.id}/action", data={"action": "mark_paid", "csrf": "bad"}).status_code == 403
    assert repo.get_booking(b.id).status == "hold"
    assert client.post(f"{BASE}/properties/new", data={"name": "X"}).status_code == 403


def test_pause_and_user_management(env):
    repo, app, _ = env
    client, token = login(app)
    client.post(f"{BASE}/conversations/%2B919847012345/pause", data={"csrf": token, "pause": "1"})
    assert repo.is_paused(PHONE)[0] is True
    client.post(f"{BASE}/users", data={"csrf": token, "email": "new@x.com", "password": "longenough1", "role": "manager",
                                       "active": "on"})
    assert repo.get_user("new@x.com").role == "manager"
    client.post(f"{BASE}/users", data={"csrf": token, "email": "short@x.com", "password": "abc", "role": "manager"})
    assert repo.get_user("short@x.com") is None


def _action(client, token, b, **data):
    return client.post(f"{BASE}/bookings/{b.id}/action", data={"csrf": token, "action": "record_payment", **data})


def test_record_cash_payment_then_balance(env):
    repo, app, (p1, _, rt1, _) = env
    b = booking(p1, rt1, 7, status="hold")
    repo.create_hold(b, now_utc())
    client, token = login(app)
    _action(client, token, b, method="cash", amount="4000", ref="", note="Paid by father")
    saved = repo.get_booking(b.id)
    assert saved.status == "paid" and saved.payment_method == "cash" and saved.amount_paid == 4000
    assert saved.payment_ref == f"RCPT-{b.id}-1" and saved.received_by == "owner@x.com"
    assert saved.balance == 4000 and saved.paid_at is not None and saved.hold_expires_at is None
    assert ANNOUNCED == [b.id]
    page = client.get(f"{BASE}/bookings/{b.id}").text
    assert saved.payment_ref in page and "Paid by father" in page and "Cash" in page and "IST" in page
    assert _action(client, token, b, method="upi", amount="9000", ref="UTR1").status_code == 200  # too much: refused
    assert repo.get_booking(b.id).amount_paid == 4000
    _action(client, token, b, method="upi", amount="4000", ref="UTR12345")
    done = repo.get_booking(b.id)
    assert done.amount_paid == 8000 and done.balance == 0 and done.payment_method == "mixed"
    assert done.payment_ref == "UTR12345" and len(done.payment_note.splitlines()) == 2
    assert ANNOUNCED == [b.id]  # the guest is told once, not for the balance


def test_upi_needs_a_reference(env):
    repo, app, (p1, _, rt1, _) = env
    b = booking(p1, rt1, 7, status="hold")
    repo.create_hold(b, now_utc())
    client, token = login(app)
    _action(client, token, b, method="upi", amount="4000", ref="")
    assert repo.get_booking(b.id).status == "hold" and not ANNOUNCED
    _action(client, token, b, method="bogus", amount="4000", ref="x")
    assert repo.get_booking(b.id).status == "hold"


def test_manual_booking_paid_at_creation(env):
    repo, app, (p1, _, rt1, _) = env
    client, token = login(app)
    d = today_ist() + timedelta(days=30)
    data = {"csrf": token, "room_type_id": rt1.id, "check_in": d.isoformat(), "check_out": (d + timedelta(days=2)).isoformat(),
            "adults": "2", "children": "0", "rooms": "1", "guest_name": "Meera", "phone": "9847012345",
            "pay_method": "upi", "pay_amount": "", "pay_ref": ""}
    client.post(f"{BASE}/bookings/new", data=data)
    assert repo.list_bookings(property_id=p1.id) == []  # UPI without a reference is refused
    client.post(f"{BASE}/bookings/new", data={**data, "pay_method": "cash", "pay_amount": "8000"})
    (b,) = repo.list_bookings(property_id=p1.id)
    assert b.status == "paid" and b.payment_method == "cash" and b.amount_paid == 8000 and b.balance == 0
    assert b.payment_ref.startswith("RCPT-") and repo.free_rooms(rt1, d, d + timedelta(days=2), now_utc()) == 0
    assert ANNOUNCED == [b.id]
    client.post(f"{BASE}/bookings/new", data={**data, "pay_method": "", "check_in": (d + timedelta(days=5)).isoformat(),
                                              "check_out": (d + timedelta(days=6)).isoformat()})
    assert sorted(x.status for x in repo.list_bookings(property_id=p1.id)) == ["hold", "paid"]


def test_calendar_month_and_day_panel(env):
    repo, app, (p1, _, rt1, _) = env
    b = booking(p1, rt1, 3)
    repo.create_hold(b, now_utc())
    client, _ = login(app)
    month = b.check_in.strftime("%Y-%m")
    page = client.get(f"{BASE}/calendar", params={"property_id": p1.id, "month": month}).text
    assert f'data-date="{b.check_in}"' in page and page.count('class="cal-day') % 7 == 0
    assert "Mon" in page and "Sun" in page and "1/1" in page  # the booked day shows 1 of 1 rooms taken
    panel = client.get(f"{BASE}/calendar/day", params={"property_id": p1.id, "date": b.check_in.isoformat()})
    assert panel.status_code == 200 and b.id in panel.text and "Asha" in panel.text
    assert f"check_in={b.check_in}" in panel.text  # the quick "new booking" link is prefilled
    empty_day = client.get(f"{BASE}/calendar/day", params={"property_id": p1.id, "date": (b.check_out + timedelta(days=1)).isoformat()})
    assert b.id not in empty_day.text
    assert client.get(f"{BASE}/calendar", params={"property_id": p1.id, "view": "timeline", "month": month}).status_code == 200
    assert client.get(f"{BASE}/calendar/day", params={"property_id": "nope", "date": "2026-01-01"}).status_code in (200, 404)


def test_csv_and_filter_have_payment_fields(env):
    repo, app, (p1, _, rt1, _) = env
    b = booking(p1, rt1, 7, status="hold")
    repo.create_hold(b, now_utc())
    other = booking(p1, rt1, 20, status="hold")
    repo.create_hold(other, now_utc())
    client, token = login(app)
    _action(client, token, b, method="cash", amount="4000")
    rows = list(csv.reader(io.StringIO(client.get(f"{BASE}/bookings/export.csv").text)))
    header = rows[0]
    for col in ("Payment method", "Payment reference", "Razorpay payment id", "Amount paid", "Balance", "Received by"):
        assert col in header
    row = next(r for r in rows[1:] if r[0] == b.id)
    assert row[header.index("Payment method")] == "Cash" and row[header.index("Amount paid")] == "4000"
    assert row[header.index("Balance")] == "4000" and row[header.index("Received by")] == "owner@x.com"
    only_cash = client.get(f"{BASE}/bookings", params={"method": "cash"}).text
    assert b.id in only_cash and other.id not in only_cash
    unpaid = client.get(f"{BASE}/bookings", params={"method": "unpaid"}).text
    assert other.id in unpaid and b.id not in unpaid
    home = client.get(f"{BASE}/").text
    assert "Collected today" in home and "Arrivals today" in home and "Chats needing" not in home
