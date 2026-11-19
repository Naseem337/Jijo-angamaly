"""Dashboard scenarios before launch (2026-11-19): auth, bookings, properties, exports and safety.

Only cases the older dashboard tests (test_dashboard / test_ops / test_leads_and_ui) do not cover.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import timedelta
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.resort.dashboard import integrations, router
from app.resort.dashboard import routes as dash_routes
from app.resort.dashboard.deps import BASE, COOKIE, get_repo, now_utc
from app.resort.dashboard.routes import today_ist
from app.resort.db import hash_password
from app.resort.models import Booking, ChatMessage, DashboardUser, Property, RoomType

PHONE = "+919847012345"
SENT: list[tuple] = []


@pytest.fixture(autouse=True)
def no_whatsapp(monkeypatch):
    SENT.clear()
    monkeypatch.setattr(integrations, "announce_paid", lambda b: SENT.append(("paid", b.id)))
    monkeypatch.setattr(integrations, "send_staff_text", lambda phone, text: SENT.append((phone, text)))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESORT_DB_PATH", str(tmp_path / "resort.sqlite3"))
    repo = get_repo()
    repo.save_user(DashboardUser("owner@x.com", hash_password("owner-pass-1"), "Owner", "owner"))
    p1 = repo.save_property(Property("Pozhiyoram Resort", advance_pct=50))
    p2 = repo.save_property(Property("Vibes Kanthalloor"))
    rt1 = repo.save_room_type(RoomType(p1.id, "Deluxe", units=2, max_adults=2, max_children=1, max_occupancy=3,
                                       base_rate=4000))
    rt2 = repo.save_room_type(RoomType(p2.id, "Cottage", units=2, base_rate=6000))
    repo.save_user(DashboardUser("mgr@x.com", hash_password("manager-pass-1"), "Mgr", "manager", [p1.id]))
    app = FastAPI()
    app.include_router(router)
    return repo, app, (p1, p2, rt1, rt2)


def login(app, email="owner@x.com", password="owner-pass-1"):
    client = TestClient(app)
    resp = client.post(f"{BASE}/login", data={"email": email, "password": password})
    assert resp.status_code == 200
    return client, re.search(r'name="csrf" value="([^"]+)"', resp.text).group(1)


def post(client, token, url, **data):
    return client.post(f"{BASE}{url}", data={"csrf": token, **data}, follow_redirects=False)


def toast(resp) -> str:
    return unquote(resp.headers.get("location", "")).replace("+", " ")


def make(repo, prop, rt, *, day=10, nights=2, status="paid", paid=4000, total=8000, name="Asha", rooms=1):
    now = now_utc()
    d = today_ist() + timedelta(days=day)
    b = Booking(prop.id, rt.id, PHONE, d, d + timedelta(days=nights), 2, 0, rooms, total, total // 2, now, status=status,
                guest_name=name, amount_paid=paid, payment_method="cash" if paid else "",
                paid_at=now if paid else None, hold_expires_at=now + timedelta(minutes=15) if status == "hold" else None)
    repo.create_hold(b, now)
    return b


# --------------------------------------------------------------------------- auth


def test_logout_and_a_forged_cookie_need_a_new_login(env):
    _, app, _ = env
    client, token = login(app)
    assert post(client, token, "/logout").status_code == 303
    assert client.get(f"{BASE}/bookings", follow_redirects=False).status_code == 303  # back to the login page
    forged = TestClient(app)
    forged.cookies.set(COOKIE, "owner@x.com.forged-signature")
    assert forged.get(f"{BASE}/bookings", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("url", ["/bookings/new", "/properties/new", "/users", "/logout"])
def test_every_post_form_needs_the_csrf_token(env, url):
    _, app, _ = env
    client, _ = login(app)
    assert client.post(f"{BASE}{url}", data={"csrf": "wrong"}, follow_redirects=False).status_code == 403
    assert client.post(f"{BASE}{url}", data={}, follow_redirects=False).status_code == 403


def test_manager_cannot_see_or_export_another_property(env):
    repo, app, (p1, p2, rt1, rt2) = env
    own, other = make(repo, p1, rt1, name="OwnGuest"), make(repo, p2, rt2, name="OtherGuest")
    client, token = login(app, "mgr@x.com", "manager-pass-1")
    export = client.get(f"{BASE}/bookings/export.csv").text
    assert own.id in export and other.id not in export
    assert client.get(f"{BASE}/calendar?property_id={p2.id}").status_code in (200, 404)
    assert "OtherGuest" not in client.get(f"{BASE}/calendar?property_id={p2.id}").text
    assert client.get(f"{BASE}/bookings/{other.id}").status_code == 404
    assert post(client, token, f"/properties/{p1.id}/delete").status_code == 403  # owners only


# --------------------------------------------------------------------------- bookings


def _new(client, token, rt, day=20, nights=2, **extra):
    d = today_ist() + timedelta(days=day)
    data = dict(room_type_id=rt.id, check_in=d.isoformat(), check_out=(d + timedelta(days=nights)).isoformat(),
                adults="2", children="0", rooms="1", guest_name="Ravi", phone="9847012345", payment="paid")
    data.update(extra)
    return post(client, token, "/bookings/new", **data)


def test_manual_booking_refuses_past_reversed_and_full_dates(env):
    repo, app, (p1, _, rt1, _) = env
    client, token = login(app)
    assert "past" in toast(_new(client, token, rt1, day=-3))
    assert "after" in toast(_new(client, token, rt1, nights=-1))
    assert "cannot hold" in toast(_new(client, token, rt1, adults="7"))
    assert _new(client, token, rt1, rooms="2").status_code == 303  # both rooms taken
    assert "Not enough free rooms" in toast(_new(client, token, rt1))
    assert len(repo.list_bookings(property_id=p1.id)) == 1


def test_walk_in_with_zero_or_negative_adults_is_kept_at_one(env):
    repo, app, (p1, _, rt1, _) = env
    client, token = login(app)
    _new(client, token, rt1, adults="-2")
    assert repo.list_bookings(property_id=p1.id)[0].adults == 1


def test_check_in_only_when_paid(env):
    repo, app, (p1, _, rt1, _) = env
    held = make(repo, p1, rt1, status="hold", paid=0)
    client, token = login(app)
    assert "Only a paid booking" in toast(post(client, token, f"/bookings/{held.id}/checkin", room_numbers="101"))
    assert repo.get_booking(held.id).status == "hold"


def test_cancel_suggests_the_owners_retention_refund_and_never_more_than_paid(env):
    repo, app, (p1, _, rt1, _) = env
    p1.cancel_policy = [{"days_before": 7, "retention_pct": 10}, {"days_before": 3, "retention_pct": 25},
                        {"days_before": 2, "retention_pct": 50}, {"days_before": 0, "retention_pct": 100}]
    repo.save_property(p1)
    b = make(repo, p1, rt1, day=10)  # total 8000, paid 4000: 10% of 8000 kept -> 3200 back
    client, token = login(app)
    assert "Policy: 80%" in client.get(f"{BASE}/bookings/{b.id}").text and "₹3,200" in client.get(f"{BASE}/bookings/{b.id}").text
    resp = post(client, token, f"/bookings/{b.id}/cancel", reason="plans changed", refund_amount="9000", method="cash")
    assert "!" in toast(resp) or repo.get_booking(b.id).status == "cancelled"
    refunded = sum(r.amount for r in repo.list_refunds(b.id))
    assert refunded <= 4000  # never more than what was paid


def test_marking_paid_from_the_dashboard_announces_to_the_guest(env):
    repo, app, (p1, _, rt1, _) = env
    held = make(repo, p1, rt1, status="hold", paid=0)
    client, token = login(app)
    post(client, token, f"/bookings/{held.id}/action", action="mark_paid")
    assert ("paid", held.id) in SENT  # the bot then sends confirmation + policy + after-booking message


# --------------------------------------------------------------------------- properties and rooms


def test_property_with_bookings_cannot_be_deleted(env):
    repo, app, (p1, _, rt1, _) = env
    make(repo, p1, rt1)
    client, token = login(app)
    assert "has bookings" in toast(post(client, token, f"/properties/{p1.id}/delete"))
    assert repo.get_property(p1.id) is not None


def test_policy_after_booking_message_and_five_retention_rows_are_saved(env):
    repo, app, (p1, _, _, _) = env
    client, token = login(app)
    post(client, token, f"/properties/{p1.id}", name="Pozhiyoram Resort", advance_pct="50", active="on",
         policies="*Cancellation Policy*\n7+ days: 10%", stay_info="Wifi - Pozhiyoram\nPass - x",
         cp_days_1="7", cp_pct_1="10", cp_days_2="3", cp_pct_2="25", cp_days_3="2", cp_pct_3="50",
         cp_days_4="0", cp_pct_4="100", cp_days_5="30", cp_pct_5="5")
    p = repo.get_property(p1.id)
    assert p.policies.startswith("*Cancellation Policy*") and p.stay_info.startswith("Wifi")
    assert [r["retention_pct"] for r in p.cancel_policy] == [5, 10, 25, 50, 100]


def test_season_price_needs_valid_dates_and_the_shorter_season_wins(env):
    repo, app, (p1, _, rt1, _) = env
    client, token = login(app)
    d = today_ist() + timedelta(days=30)
    bad = post(client, token, f"/properties/{p1.id}/rooms/{rt1.id}/rates", start=d.isoformat(),
               end=(d - timedelta(days=1)).isoformat(), rate="5000")
    assert "valid dates" in toast(bad) and repo.list_rate_rules(rt1.id) == []
    post(client, token, f"/properties/{p1.id}/rooms/{rt1.id}/rates", start=d.isoformat(),
         end=(d + timedelta(days=20)).isoformat(), rate="5000", label="Season")
    post(client, token, f"/properties/{p1.id}/rooms/{rt1.id}/rates", start=(d + timedelta(days=5)).isoformat(),
         end=(d + timedelta(days=6)).isoformat(), rate="9000", label="Festival")
    quote = client.get(f"{BASE}/api/quote", params={"room_type_id": rt1.id, "check_in": (d + timedelta(days=5)).isoformat(),
                       "check_out": (d + timedelta(days=6)).isoformat(), "adults": "2", "children": "0"}).json()
    assert quote["total"] == 9000


def _png() -> bytes:
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "app" / "resort" / "dashboard" / "static" / "icon-192.png").read_bytes()


def test_photos_stop_at_the_limit_and_say_so(env, monkeypatch):
    repo, app, (p1, _, _, _) = env
    monkeypatch.setattr(dash_routes, "MAX_PHOTOS", 3)
    client, token = login(app)
    url = f"{BASE}/properties/{p1.id}/photos"
    files = [("photos", (f"{i}.png", _png(), "image/png")) for i in range(5)]
    first = client.post(url, data={"csrf": token, "action": "upload"}, files=files, follow_redirects=False)
    assert len(repo.get_property(p1.id).photo_urls) == 3 and "only 3 could be added" in toast(first)
    full = client.post(url, data={"csrf": token, "action": "upload"}, files=files[:1], follow_redirects=False)
    assert "already has 3 photos" in toast(full) and len(repo.get_property(p1.id).photo_urls) == 3


# --------------------------------------------------------------------------- exports, safety, chats


def test_csv_export_is_excel_ready_with_native_names_and_no_formula_injection(env):
    repo, app, (p1, _, rt1, _) = env
    make(repo, p1, rt1, name="അനു", day=5)
    make(repo, p1, rt1, name="=HYPERLINK(\"http://x\")", day=15)
    client, _ = login(app)
    resp = client.get(f"{BASE}/bookings/export.csv")
    assert resp.text.startswith(chr(0xFEFF)) and "charset=utf-8" in resp.headers["content-type"]
    rows = list(csv.reader(io.StringIO(resp.text.lstrip(chr(0xFEFF)))))
    names = [r[rows[0].index("Guest")] for r in rows[1:]]
    assert "അനു" in names and "'=HYPERLINK(\"http://x\")" in names  # a leading ' stops Excel running it


def test_a_script_in_a_guest_name_is_escaped_on_every_page(env):
    repo, app, (p1, _, rt1, _) = env
    evil = "<script>alert(1)</script>"
    b = make(repo, p1, rt1, name=evil)
    repo.touch_guest(PHONE, now_utc(), name=evil)
    repo.add_chat(ChatMessage(PHONE, "in", evil, now_utc()))
    client, _ = login(app)
    for url in ("/", "/bookings", f"/bookings/{b.id}", "/guests", "/conversations",
                f"/conversations/{PHONE.replace('+', '%2B')}", "/search?q=script", "/stay"):
        assert evil not in client.get(f"{BASE}{url}").text, url


def test_bookings_list_pages_100_at_a_time(env, monkeypatch):
    repo, app, (p1, _, rt1, _) = env
    monkeypatch.setattr(dash_routes, "BOOKINGS_PER_PAGE", 2)
    ids = [make(repo, p1, rt1, day=d, nights=1, name=f"G{d}").id for d in (5, 7, 9)]
    client, _ = login(app)
    first, second = client.get(f"{BASE}/bookings").text, client.get(f"{BASE}/bookings?page=2").text
    assert "page 1 of 2" in first and "Next" in first and "page 2 of 2" in second
    assert sum(i in first for i in ids) == 2 and sum(i in second for i in ids) == 1


def test_a_staff_reply_from_the_dashboard_pauses_the_bot(env):
    repo, app, _ = env
    client, token = login(app)
    post(client, token, f"/conversations/{PHONE.replace('+', '%2B')}/send", text="I will call you, sir")
    assert (PHONE, "I will call you, sir") in SENT and repo.is_paused(PHONE)[0]


def test_a_long_chat_and_search_still_render(env):
    repo, app, (p1, _, rt1, _) = env
    for i in range(400):
        repo.add_chat(ChatMessage(PHONE, "in" if i % 2 else "out", f"message {i}", now_utc()))
    make(repo, p1, rt1, name="Searchable Name")
    client, _ = login(app)
    assert client.get(f"{BASE}/conversations/{PHONE.replace('+', '%2B')}").status_code == 200
    assert "Searchable Name" in client.get(f"{BASE}/search?q=searchable").text
