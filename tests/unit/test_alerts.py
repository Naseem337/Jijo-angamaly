"""Step 10: the alert engine (matching, seeding, idempotency, caps, consent, window, dry run)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from app.alerts.engine import SEEDED_AT, AlertEngine
from app.alerts.matcher import car_line, match_car, wish_from_requirement
from app.core.client_config import NearMatchRules, load_client_config
from app.core.clock import IST, FixedClock
from app.inventory import Car, CarQuery, InventoryAdapter, InventoryError
from app.inventory.aliases import ModelAliases
from app.providers.images import ImageError
from app.storage.models import Lead, LeadRequirement, Requirement
from app.storage.sqlite_repo import SQLiteRepository
from app.whatsapp.client import WhatsAppClient, WhatsAppError

CID = "truechoice"
PHONE = "+919847012345"
OTHER = "+919847099999"
PHOTO = "https://example.com/car.webp"


def car(car_id: str, *, model: str = "Swift", price: int = 550_000, year: int = 2020, km: int = 40_000,
        fuel: str = "petrol", transmission: str | None = "manual", image: str | None = PHOTO) -> Car:
    return Car(id=car_id, make="Maruti Suzuki", model=model, variant="VXi", year=year, price=price, km=km,
               fuel=fuel, transmission=transmission, location="Pattambi", image_url=image)


class Stock(InventoryAdapter):
    def __init__(self, cars: list[Car]) -> None:
        self.cars = list(cars)
        self.error: str | None = None

    def search_cars(self, query: CarQuery) -> list[Car]:
        return []

    def list_all_cars(self) -> list[Car]:
        if self.error:
            raise InventoryError(self.error)
        return [c for c in self.cars if c.is_available]


class FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail: str | None = None

    def send_template(self, to, name, language, params, *, header_image_id=None) -> str:
        if self.fail:
            raise WhatsAppError(self.fail)
        self.sent.append({"to": to, "name": name, "language": language, "params": params, "header": header_image_id})
        return f"wamid.alert{len(self.sent)}"


class FakePhotos:
    def media_id(self, c: Car) -> str:
        if not c.image_url:
            raise ImageError(f"car {c.id} has no photo")
        return f"media-{c.id}"


@pytest.fixture
def env(clients_dir: Path, tmp_path: Path, clock: FixedClock):
    config = load_client_config(CID, clients_dir)
    repo = SQLiteRepository(tmp_path / "alerts.db")
    stock = Stock([car("TC-OLD001", model="Baleno")])
    wa = FakeWhatsApp()
    engine = AlertEngine(config, repo, lambda: stock, wa, clock, photos=lambda: FakePhotos())
    assert engine.run().seeded == 1  # first run: existing stock recorded, nothing sent
    yield engine, repo, stock, wa, clock
    repo.close()


def add_lead(repo, clock, phone: str = PHONE, status: str = "active", **want) -> Lead:
    now = clock.now()
    repo.get_or_create_customer(CID, phone, now)
    want = want or {"models": ["swift"], "budget_max": 600_000, "fuel": "petrol"}
    lead = Lead(CID, phone, now, now + timedelta(days=90), "+919447000000", LeadRequirement(**want),
                status=status, consent_at=now if status == "active" else None)
    repo.create_lead(lead)
    return lead


def arrive(stock: Stock, clock: FixedClock, *cars: Car) -> None:
    clock.advance(hours=1)
    stock.cars.extend(cars)


# ---------------------------------------------------------------------------- matching

def test_match_rules_exact_near_and_not_both() -> None:
    aliases, near = ModelAliases(), NearMatchRules()
    want = LeadRequirement(models=["swift"], budget_max=600_000, min_year=2019, fuel="petrol")
    assert match_car(car("A"), want, aliases, near) == "match"
    assert match_car(car("A", price=650_000), want, aliases, near) == "near"  # ≤ 10 % over
    assert match_car(car("A", price=700_000), want, aliases, near) is None  # too far over
    assert match_car(car("A", year=2018), want, aliases, near) == "near"  # 1 year older
    assert match_car(car("A", year=2017), want, aliases, near) is None
    assert match_car(car("A", price=650_000, year=2018), want, aliases, near) is None  # not both
    assert match_car(car("A", fuel="diesel"), want, aliases, near) is None
    assert match_car(car("A", model="Baleno"), want, aliases, near) is None


def test_alerts_are_strict_about_unlisted_gearbox_and_other_criteria() -> None:
    aliases, near = ModelAliases(), NearMatchRules()
    auto = LeadRequirement(models=["swift"], gearbox="automatic")
    assert match_car(car("A", transmission=None), auto, aliases, near) is None  # unknown ≠ automatic
    assert match_car(car("A", transmission="automatic"), auto, aliases, near) == "match"
    assert match_car(car("A", km=70_000), LeadRequirement(max_km=60_000), aliases, near) is None
    assert match_car(car("A"), LeadRequirement(makes=["Maruti"]), aliases, near) == "match"
    assert match_car(car("A"), LeadRequirement(makes=["Hyundai"]), aliases, near) is None
    assert match_car(car("A"), LeadRequirement(notes="anything"), aliases, near) is None  # nothing to match on


def test_car_line_is_one_line_with_near_label_and_id() -> None:
    want = LeadRequirement(budget_max=600_000)
    line = car_line(car("TC-A72F0B", price=650_000), "near", want)
    assert line == ("Maruti Suzuki Swift VXi 2020, 40,000 km, Petrol, ₹6.5 lakh (slightly above your budget), "
                    "at Pattambi (TC-A72F0B)")
    assert "\n" not in line


def test_chat_requirement_converts_to_a_wish(clock) -> None:
    req = Requirement(CID, PHONE, clock.now(), clock.now(), budget_max=500_000, make="Maruti", model="swift")
    assert wish_from_requirement(req) == LeadRequirement(budget_max=500_000, makes=["Maruti"], models=["swift"])


# ---------------------------------------------------------------------------- the engine

def test_first_run_only_records_existing_stock(env) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock, models=["baleno"])
    report = engine.run()
    assert (report.sent, report.recent_cars, wa.sent) == (0, 0, [])  # the old Baleno is never "new"
    assert repo.get_seen_cars(CID) == {"TC-OLD001": SEEDED_AT}


def test_new_matching_car_sends_one_car_template_with_photo(env) -> None:
    engine, repo, stock, wa, clock = env
    lead = add_lead(repo, clock)
    arrive(stock, clock, car("TC-NEW001"))
    report = engine.run()
    assert report.sent == 1 and report.new_cars == 1
    [msg] = wa.sent
    assert (msg["to"], msg["name"], msg["language"], msg["header"]) == (PHONE, "tc_car_alert_1", "en", "media-TC-NEW001")
    assert msg["params"] == ["Maruti Suzuki Swift VXi 2020, 40,000 km, Petrol, ₹5.5 lakh, at Pattambi (TC-NEW001)"]
    [alert] = repo.list_alerts(CID, phone=PHONE)
    assert (alert.status, alert.kind, alert.requirement_id, alert.wa_message_id) == (
        "sent", "match", f"lead:{lead.id}", "wamid.alert1")
    assert repo.get_lead(CID, lead.id).status == "notified"
    stored = repo.list_messages(CID, PHONE)[-1]
    assert stored.source == "template" and "TC-NEW001" in stored.text and "₹5.5 lakh" in stored.text

    clock.advance(days=1)
    assert engine.run().sent == 0 and len(wa.sent) == 1  # never the same car twice


def test_several_cars_go_in_one_message_best_first_no_photo(env) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock)
    arrive(stock, clock, car("TC-A", price=450_000), car("TC-B", price=590_000), car("TC-C", price=520_000),
           car("TC-D", price=500_000))
    engine.run()
    [msg] = wa.sent
    assert msg["name"] == "tc_car_alert_3" and msg["header"] is None
    assert [p.rsplit("(", 1)[1] for p in msg["params"]] == ["TC-B)", "TC-C)", "TC-D)"]  # closest to budget first
    clock.advance(days=1)
    engine.run()
    assert wa.sent[1]["name"] == "tc_car_alert_1" and "TC-A" in wa.sent[1]["params"][0]  # the 4th, next day


def test_one_message_per_customer_per_day(env) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock)
    arrive(stock, clock, car("TC-A"))
    engine.run()
    arrive(stock, clock, car("TC-B"))
    report = engine.run()
    assert len(wa.sent) == 1 and report.skipped["already alerted today"] == 1
    clock.advance(days=1)
    engine.run()
    assert len(wa.sent) == 2  # TC-B is still "new" the next day


def test_only_cars_that_arrived_after_the_lead(env) -> None:
    engine, repo, stock, wa, clock = env
    arrive(stock, clock, car("TC-A"))
    engine.run()  # TC-A recorded before the customer's visit
    clock.advance(hours=1)
    add_lead(repo, clock)
    clock.advance(hours=1)
    assert engine.run().sent == 0 and wa.sent == []


def test_who_never_gets_alerts(env) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock, PHONE, status="pending_consent")  # never said YES
    add_lead(repo, clock, OTHER)
    repo.set_bot_paused(CID, OTHER, True, at=clock.now(), reason="human")  # a salesperson owns the chat
    arrive(stock, clock, car("TC-A"))
    report = engine.run()
    assert wa.sent == [] and report.skipped["chat is with a salesperson"] == 1

    repo.set_bot_paused(CID, OTHER, False, at=clock.now(), reason="resume")
    repo.set_opted_out(CID, OTHER, clock.now())
    report = engine.run()
    assert wa.sent == [] and report.skipped["opted out"] == 1


def test_chat_requirement_needs_customer_consent(env) -> None:
    engine, repo, stock, wa, clock = env
    now = clock.now()
    repo.get_or_create_customer(CID, PHONE, now)
    repo.create_requirement(Requirement(CID, PHONE, now, now + timedelta(days=60), model="swift", budget_max=600_000))
    arrive(stock, clock, car("TC-A"))
    assert engine.run().skipped["no consent"] == 1 and wa.sent == []
    repo.set_consent(CID, PHONE, source="chat", recorded_by="customer", at=clock.now())
    engine.run()
    assert len(wa.sent) == 1


def test_outside_send_window_waits_and_sends_later(env) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock)
    clock.set(datetime(2026, 10, 16, 21, 30, tzinfo=IST))
    stock.cars.append(car("TC-A"))
    report = engine.run()
    assert report.deferred and wa.sent == []
    clock.set(datetime(2026, 10, 17, 9, 15, tzinfo=IST))
    assert engine.run().sent == 1


@pytest.mark.parametrize("problem", ["error", "empty"])
def test_stock_outage_aborts_and_marks_nothing_seen(env, problem) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock)
    if problem == "error":
        stock.error = "HTTP 500"
    else:
        stock.cars = []
    report = engine.run()
    assert report.aborted and wa.sent == []
    assert set(repo.get_seen_cars(CID)) == {"TC-OLD001"}


def test_dry_run_sends_nothing_and_a_real_run_still_can(env) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock)
    arrive(stock, clock, car("TC-A"))
    report = engine.run(dry_run=True)
    assert wa.sent == [] and report.lines == ["+91******2345: would send 1 car(s): TC-A"]
    assert [a.status for a in repo.list_alerts(CID, phone=PHONE)] == ["dry_run"]
    assert engine.run().sent == 1 and [a.status for a in repo.list_alerts(CID, phone=PHONE)] == ["sent"]


def test_failed_send_is_retried_next_run(env) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock)
    arrive(stock, clock, car("TC-A"))
    wa.fail = "HTTP 400: template not approved"
    assert engine.run().failed == 1
    assert repo.list_alerts(CID, phone=PHONE)[0].status == "failed"
    wa.fail = None
    clock.advance(days=1)
    assert engine.run().sent == 1


def test_one_car_without_photo_fails_instead_of_sending_a_broken_template(env) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock)
    arrive(stock, clock, car("TC-A", image=None))
    report = engine.run()
    assert report.failed == 1 and wa.sent == [] and "photo" in report.lines[0]


def test_at_most_one_near_match_per_week(env) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock)  # Swift up to ₹6 lakh
    arrive(stock, clock, car("TC-N1", price=640_000), car("TC-N2", price=630_000))
    engine.run()
    assert len(wa.sent) == 1 and "slightly above your budget" in wa.sent[0]["params"][0]
    clock.advance(days=1)
    assert engine.run().sent == 0  # the other near match waits a week
    clock.advance(days=6, hours=1)
    stock.cars.append(car("TC-N3", price=620_000))
    assert engine.run().sent == 1


def test_alerts_pause_after_three_without_a_reply(env) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock)
    for i in range(4):
        arrive(stock, clock, car(f"TC-{i}"))
        engine.run()
        clock.advance(days=1)
    assert len(wa.sent) == 3
    customer = repo.get_customer(CID, PHONE)
    customer.last_inbound_at = clock.now()  # the customer wrote back
    repo.save_customer(customer)
    clock.advance(hours=1)
    assert engine.run().sent == 1


def test_pretend_new_lets_staff_test_with_a_car_already_in_stock(env) -> None:
    engine, repo, stock, wa, clock = env
    add_lead(repo, clock, models=["baleno"])
    clock.advance(hours=1)
    assert engine.run(pretend_new=frozenset({"TC-OLD001"})).sent == 1
    assert repo.get_seen_cars(CID)["TC-OLD001"] == SEEDED_AT  # not changed for real


# ---------------------------------------------------------------------------- WhatsApp payload

def test_send_template_puts_the_photo_in_the_header() -> None:
    url = "https://graph.facebook.com/v25.0/123/messages"
    with respx.mock() as graph:
        route = graph.post(url).mock(return_value=httpx.Response(200, json={"messages": [{"id": "wamid.1"}]}))
        WhatsAppClient("123", "token").send_template(PHONE, "tc_car_alert_1", "en", ["line"], header_image_id="m1")
    body = json.loads(route.calls[0].request.content)
    assert body["template"]["components"] == [
        {"type": "header", "parameters": [{"type": "image", "image": {"id": "m1"}}]},
        {"type": "body", "parameters": [{"type": "text", "text": "line"}]}]
