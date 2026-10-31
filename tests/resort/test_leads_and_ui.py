"""Leads saved from WhatsApp, the Guests page, the simpler home page and settings, and the old stand-in text cleanup."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

import pytest

from app.resort import leads as leads_mod
from app.resort import seed
from app.resort.dashboard.deps import BASE, get_repo, now_utc
from app.resort.db import ResortRepo
from app.resort.models import Guest, Property, RoomType
from tests.resort.test_ops import env, login, make, no_whatsapp  # noqa: F401  (fixtures)

PHONE = "+919847012345"


def _lead(repo, phone=PHONE, **fields):
    repo.upsert_guest(Guest(phone, name=fields.pop("name", "Asha"), last_seen=now_utc()))
    leads_mod.save_lead(repo, phone, now_utc(), **fields)


def test_save_lead_keeps_old_values_and_overwrites_new_ones(tmp_path):
    repo = ResortRepo(tmp_path / "r.sqlite3")
    leads_mod.save_lead(repo, PHONE, now_utc(), property_id="P-1", check_in="2026-12-01", check_out="2026-12-03",
                        adults=2, rooms=1, stage="asked")
    leads_mod.save_lead(repo, PHONE, now_utc(), room_type="Deluxe", stage="summary", from_city="Kochi")
    lead = leads_mod.get_lead(repo, PHONE)
    assert (lead["property_id"], lead["check_in"], lead["adults"], lead["room_type"]) == ("P-1", "2026-12-01", 2, "Deluxe")
    assert (lead["stage"], lead["from_city"]) == ("summary", "Kochi")
    assert list(leads_mod.list_leads(repo)) == [PHONE]


def test_guests_page_lists_leads_with_status_and_filters(env):
    repo, app, (p1, p2, rt1, rt2) = env
    _lead(repo, "+919800000001", name="Maya", property_id=p1.id, check_in="2026-12-05", check_out="2026-12-07",
          adults=2, rooms=1, stage="asked", from_city="Kochi")
    booking = make(repo, p1, rt1, status="paid")  # PHONE has a paid booking
    _lead(repo, PHONE, name="Asha", property_id=p1.id, check_in="2026-12-10", check_out="2026-12-12", adults=2, rooms=1,
          stage="held", booking_id=booking.id)
    repo.upsert_guest(Guest("+919800000002", name="Ravi", last_seen=now_utc() - timedelta(days=1)))  # only said hi
    client, _ = login(app)
    page = client.get(f"{BASE}/guests").text
    assert "Guests &amp; leads" in page and "Maya" in page and "Kochi" in page
    assert "Asked for dates" in page and "Booked" in page and "Just chatting" in page
    assert "05/12 to 07/12" in page
    interested = client.get(f"{BASE}/guests", params={"stage": "interested"}).text
    assert "Maya" in interested and "Ravi" not in interested and "Asha" not in interested
    assert "Kochi" in client.get(f"{BASE}/guests", params={"q": "kochi"}).text
    rows = list(csv.reader(io.StringIO(client.get(f"{BASE}/guests?format=csv").text)))
    assert rows[0][:5] == ["Name", "Phone", "Language", "Place", "Check-in"]
    maya = next(r for r in rows[1:] if r[0] == "Maya")
    assert maya[3] == "Pozhiyoram Resort" and maya[4] == "2026-12-05" and maya[10] == "Kochi" and maya[11] == "Asked for dates"


def test_manager_sees_only_leads_for_their_property(env):
    repo, app, (p1, p2, rt1, rt2) = env
    _lead(repo, "+919800000001", name="Maya", property_id=p1.id, check_in="2026-12-05", check_out="2026-12-07",
          adults=2, stage="asked")
    _lead(repo, "+919800000003", name="Nair", property_id=p2.id, check_in="2026-12-05", check_out="2026-12-07",
          adults=2, stage="asked")
    mgr, _ = login(app, "mgr@x.com", "manager-pass-1")
    page = mgr.get(f"{BASE}/guests").text
    assert "Nair" in page and "Maya" not in page


def test_guest_page_shows_the_latest_enquiry(env):
    repo, app, (p1, *_rest) = env
    _lead(repo, property_id=p1.id, check_in="2026-12-05", check_out="2026-12-07", adults=3, rooms=2, room_type="Deluxe",
          stage="summary", from_city="Chennai")
    client, _ = login(app)
    page = client.get(f"{BASE}/guests/{PHONE.replace('+', '%2B')}").text
    assert "Latest enquiry" in page and "Chennai" in page and "Deluxe" in page


def test_home_is_the_simple_front_desk_view(env):
    repo, app, (p1, _p2, rt1, _rt2) = env
    make(repo, p1, rt1, status="hold", paid=0)
    client, _ = login(app)
    home = client.get(f"{BASE}/").text
    for want in ("Arrivals today", "Departures today", "In the house", "Occupancy tonight", "Collected today",
                 "Waiting for payment", "Next 14 days"):
        assert want in home, want
    assert home.count("New booking") == 1  # one button, not two
    for gone in ("Open chats", "Chats needing a human", "Money collected, by how it was paid", "all time"):
        assert gone not in home, gone
    assert "WhatsApp chats" not in home  # not in the menu either


def test_settings_lists_only_what_is_needed(env):
    _repo, app, _ = env
    client, _ = login(app)
    page = client.get(f"{BASE}/settings").text
    for need in ("RESORT_WHATSAPP_ACCESS_TOKEN", "RAZORPAY_KEY_ID", "GEMINI_API_KEY", "SESSION_SECRET"):
        assert need in page
    for extra in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "RESORT_DB_PATH"):
        assert extra not in page


def test_collections_has_one_date_range_field_and_no_help_clutter(env):
    repo, app, (p1, *_rest) = env
    client, _ = login(app)
    page = client.get(f"{BASE}/reports/collections").text
    assert "data-range" in page and 'name="from"' in page and 'name="to"' in page and 'type="date"' not in page
    assert "Last 7 days" in page and "This month" in page
    detail = client.get(f"{BASE}/properties/{p1.id}").text
    for gone in ("Press Enter or Add", "talk to a person", "still want to negotiate"):
        assert gone not in detail, gone
    assert "Click a day to see who is staying" not in client.get(f"{BASE}/calendar").text


def test_old_stand_in_text_is_removed_from_existing_data(tmp_path):
    repo = ResortRepo(tmp_path / "r.sqlite3")
    pz = repo.save_property(Property("Pozhiyoram Resort", description="PLACEHOLDER description: replace with the real one."))
    kt = repo.save_property(Property("Vibes Kanthalloor", description="PLACEHOLDER description: replace with the real one."))
    for prop in (pz, kt):
        repo.save_room_type(RoomType(prop.id, "Deluxe Room (placeholder)", units=3, base_rate=4500, description="PLACEHOLDER"))
    lines = seed.seed(repo)
    assert any("Upgraded" in line for line in lines)
    props = {p.name: p for p in repo.list_properties(active_only=False)}
    assert props["Pozhiyoram Resort"].active and not props["Vibes Kanthalloor"].active
    every = [p.description for p in props.values()]
    for prop in props.values():
        every += [r.name + " " + r.description for r in repo.list_room_types(prop.id, active_only=False)]
    assert not any("placeholder" in text.lower() for text in every)
    assert [r.name for r in repo.list_room_types(props["Vibes Kanthalloor"].id, active_only=False)] == ["Deluxe Room"]
    assert [r.name for r in repo.list_room_types(props["Pozhiyoram Resort"].id)] == ["Beach View AC Room"]
    assert seed.seed(repo)[0] == "Properties already exist: nothing seeded."  # a second run changes nothing


def test_fresh_seed_has_no_stand_in_wording_and_only_alleppey_is_open(tmp_path):
    repo = ResortRepo(tmp_path / "r.sqlite3")
    seed.seed(repo)
    assert [p.name for p in repo.list_properties()] == ["Pozhiyoram Resort"]
    kanthalloor = next(p for p in repo.list_properties(active_only=False) if not p.active)
    texts = [kanthalloor.description] + [r.name + r.description for r in repo.list_room_types(kanthalloor.id, active_only=False)]
    assert not any("placeholder" in t.lower() for t in texts)


def test_owner_login_comes_from_the_settings(tmp_path, monkeypatch):
    from app.resort.db import check_password
    repo = ResortRepo(tmp_path / "r.sqlite3")
    monkeypatch.setenv("RESORT_OWNER_EMAIL", "Manager@Gmail.com")
    monkeypatch.setenv("RESORT_OWNER_PASSWORD", "1234")
    assert any("manager@gmail.com" in line for line in seed.seed(repo))
    user = repo.get_user("manager@gmail.com")
    assert user.role == "owner" and check_password("1234", user.password_hash)
    assert repo.get_user("owner@resort.local") is None
    monkeypatch.setenv("RESORT_OWNER_PASSWORD", "5678")  # changing the setting changes the login at the next start
    assert any("updated" in line for line in seed.seed(repo))
    assert check_password("5678", repo.get_user("manager@gmail.com").password_hash)
    assert "Owner user already exists." in seed.seed(repo)


def test_pages_link_a_versioned_stylesheet_so_browsers_never_show_an_old_design(env):
    _repo, app, _ = env
    client, _ = login(app)
    home = client.get(f"{BASE}/").text
    assert "static/app.css?v=" in home and "static/app.js?v=" in home and "Good " in home and "🌴" in home
    assert client.get(f"{BASE}/static/app.css").headers["cache-control"] == "no-cache"
