from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.clock import IST, FixedClock
from app.providers.llm import LLMError, MockLLM
from app.resort import web
from app.resort.config import load_resort_config
from app.resort.db import ResortRepo
from app.resort.flow import BookingBot, parse_date
from app.resort.i18n import S, detect_script, t
from app.resort.models import Booking, Property, RoomType
from app.resort.payments import MockPayments, parse_event, verify_webhook_signature
from app.resort.services import ResortServices
from app.whatsapp.client import WhatsAppClient, WhatsAppError
from app.whatsapp.parse import InboundMessage, parse_webhook

STAFF = "+919800000001"
GUEST = "+919847012345"
_ids = itertools.count(1)


class FakeWA:
    """Records everything the bot sends."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, object]] = []  # (to, kind, payload)
        self.text_error: WhatsAppError | None = None

    def send_text(self, to, text):
        if self.text_error and to != STAFF:
            raise self.text_error
        self.sent.append((to, "text", text))
        return "wamid.x"

    def send_interactive_list(self, to, body, button, rows, **kw):
        self.sent.append((to, "list", {"body": body, "button": button, "rows": rows}))
        return "wamid.x"

    def send_reply_buttons(self, to, body, buttons):
        self.sent.append((to, "buttons", {"body": body, "buttons": buttons}))
        return "wamid.x"

    def send_template(self, to, name, language, params, **kw):
        self.sent.append((to, "template", {"name": name, "language": language, "params": params}))
        return "wamid.x"

    def mark_read(self, *a, **k):
        return True

    def to(self, phone):
        return [s for s in self.sent if s[0] == phone]

    def last(self, phone=GUEST):
        return self.to(phone)[-1]

    def texts(self, phone=GUEST):
        return [p for _, k, p in self.to(phone) if k == "text"]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec")
    repo = ResortRepo(tmp_path / "r.sqlite3")
    p1 = repo.save_property(Property("Pozhiyoram Resort", city="Kerala", advance_pct=50, staff_numbers=[STAFF],
                                     policies="Free cancellation 7 days before."))
    repo.save_property(Property("Vibes Kanthalloor", city="Munnar", advance_pct=50))
    deluxe = repo.save_room_type(RoomType(p1.id, "Deluxe", units=2, max_adults=2, max_children=1, max_occupancy=3,
                                          base_rate=4000))
    family = repo.save_room_type(RoomType(p1.id, "Family Suite", units=3, max_adults=4, max_children=2,
                                          max_occupancy=6, base_rate=8000))
    clock = FixedClock(datetime(2026, 11, 2, 10, 0, tzinfo=IST))  # a Monday
    wa, pay = FakeWA(), MockPayments()
    llm = MockLLM()
    bot = BookingBot(load_resort_config(), repo, wa, pay, clock, llm=lambda: llm)
    return type("W", (), dict(repo=repo, wa=wa, pay=pay, clock=clock, bot=bot, llm=llm, p1=p1, deluxe=deluxe,
                              family=family))()


def msg(phone, text="", rid=None, kind=None):
    return InboundMessage(f"wamid.{next(_ids)}", phone, kind or ("interactive" if rid else "text"),
                          datetime(2026, 11, 2, 4, 30), text=text or (rid or ""), reply_id=rid)


def tap(w, rid, phone=GUEST):
    return w.bot.handle(msg(phone, rid=rid))


def say(w, text, phone=GUEST):
    return w.bot.handle(msg(phone, text))


def go_to_confirm(w, adults=2, children=1, nights=2, rt="deluxe", rooms=None, phone=GUEST):
    say(w, "Hi", phone)
    tap(w, "lang:en", phone)
    tap(w, "menu:book", phone)
    tap(w, f"prop:{w.p1.id}", phone)
    tap(w, "d:2026-11-10", phone)
    tap(w, f"n:{nights}", phone)
    tap(w, f"a:{adults}", phone)
    tap(w, f"c:{children}", phone)
    tap(w, f"rt:{getattr(w, rt).id}", phone)
    if rooms:
        tap(w, f"r:{rooms}", phone)


# --------------------------------------------------------------------------- the booking flow


def test_full_tap_flow_to_payment_link(world):
    w = world
    assert say(w, "Hi") == "language_picker"
    kind, payload = w.wa.last()[1:]
    assert kind == "list" and [r[0] for r in payload["rows"]] == ["lang:en", "lang:ml", "lang:ta", "lang:hi"]
    assert tap(w, "lang:en") == "menu"
    assert [r[0] for r in w.wa.last()[2]["rows"]] == ["menu:book", "menu:ask", "menu:status", "menu:staff"]
    tap(w, "menu:book")
    assert [r[1] for r in w.wa.last()[2]["rows"]] == ["Pozhiyoram Resort", "Vibes Kanthalloor"]
    tap(w, f"prop:{w.p1.id}")
    rows = w.wa.last()[2]["rows"]
    assert rows[0][1] == "Today" and rows[1][1] == "Tomorrow" and rows[-1][0] == "d:other" and len(rows) == 10
    tap(w, "d:2026-11-10")
    assert w.wa.last()[2]["rows"][-1][0] == "n:more"
    tap(w, "n:2")
    assert w.wa.last()[2]["rows"][0][0] == "a:1"
    tap(w, "a:2")
    assert w.wa.last()[2]["rows"][0][0] == "c:0"
    tap(w, "c:1")
    types = w.wa.last()[2]["rows"]
    assert [r[0] for r in types] == [f"rt:{w.deluxe.id}", f"rt:{w.family.id}"]
    assert "8,000" in types[0][2] and "16,000" in types[1][2]  # 2 nights x rate, whole stay
    tap(w, f"rt:{w.deluxe.id}")
    assert [r[0] for r in w.wa.last()[2]["rows"]] == ["r:1", "r:2"]
    tap(w, "r:1")
    kind, payload = w.wa.last()[1:]
    assert kind == "buttons" and [b[0] for b in payload["buttons"]] == ["ok", "chg"]
    assert "Total: ₹8,000" in payload["body"] and "₹4,000" in payload["body"]
    tap(w, "ok")
    assert "guest name" in w.wa.last()[2]
    assert say(w, "Asha Menon") == "payment_link"
    assert "https://rzp.io/mock/" in w.wa.texts()[-1] and "4,000" in w.wa.texts()[-1]
    booking = w.repo.list_bookings(phone=GUEST)[0]
    assert (booking.status, booking.advance, booking.total, booking.rooms) == ("hold", 4000, 8000, 1)
    assert booking.guest_name == "Asha Menon" and booking.payment_link_url
    assert w.pay.created[0]["amount"] == 4000


def test_returning_guest_with_name_skips_name_question(world):
    w = world
    w.repo.touch_guest(GUEST, w.clock.now(), name="Asha")
    go_to_confirm(w, rooms=1)
    assert tap(w, "ok") == "payment_link"


def test_ten_guests_never_one_room(world):
    w = world
    say(w, "Hi")
    tap(w, "lang:en")
    tap(w, "menu:book")
    tap(w, f"prop:{w.p1.id}")
    tap(w, "d:2026-11-10")
    tap(w, "n:1")
    tap(w, "a:more")
    assert "type the number of adults" in w.wa.last()[2]
    say(w, "10")
    tap(w, "c:0")
    # Family Suite fits 4 adults: 10 people need 3 rooms (3 free), never 1 or 2. Deluxe can't take them.
    types = w.wa.last()[2]["rows"]
    assert [r[0] for r in types] == [f"rt:{w.family.id}"] and "3 room" in types[0][2]
    tap(w, f"rt:{w.family.id}")
    assert w.wa.last()[1] == "buttons"  # only one valid count (3): no room question, straight to the summary
    assert w.repo.get_state(GUEST)["rooms"] == 3


def test_rooms_list_only_offers_valid_counts_and_rejects_others(world):
    w = world
    go_to_confirm(w, adults=8, children=0, nights=1, rt="family")
    assert [r[0] for r in w.wa.last()[2]["rows"]] == ["r:2", "r:3"]
    assert tap(w, "r:1") == "rooms"  # not offered: refused and the list is shown again
    assert w.repo.get_state(GUEST)["step"] == "rooms" and "rooms" not in w.repo.get_state(GUEST)
    tap(w, "r:2")
    assert w.repo.get_state(GUEST)["rooms"] == 2


def test_no_room_fits_offers_change_or_team(world):
    w = world
    go_to_confirm(w, adults=20, children=0, nights=1, rt="family")
    kind, payload = w.wa.last()[1:]
    assert kind == "buttons" and [b[0] for b in payload["buttons"]] == ["nf_guests", "nf_dates", "nf_staff"]
    assert w.repo.get_state(GUEST)["step"] == "nofit"
    tap(w, "nf_staff")
    assert w.repo.is_paused(GUEST)[0]
    assert "no room fits" in w.wa.texts(STAFF)[-1]


def test_soldout_race_offers_choosing_again(world):
    w = world
    w.repo.touch_guest(GUEST, w.clock.now(), name="Asha")
    go_to_confirm(w, rooms=2)
    other = Booking(w.p1.id, w.deluxe.id, "+919811111111", date(2026, 11, 10), date(2026, 11, 12), 2, 0, 2, 8000,
                    4000, w.clock.now(), hold_expires_at=w.clock.now() + timedelta(minutes=15))
    w.repo.create_hold(other, w.clock.now())  # someone else grabbed both Deluxe rooms first
    assert tap(w, "ok") == "type"
    assert "just taken" in w.wa.texts()[-1]
    assert [b.id for b in w.repo.list_bookings(phone=GUEST)] == []
    assert [r[0] for r in w.wa.last()[2]["rows"]] == [f"rt:{w.family.id}"]  # fresh list: Deluxe is gone


def test_typed_date_and_numbers_are_accepted(world):
    w = world
    say(w, "Hi")
    tap(w, "lang:en")
    tap(w, "menu:book")
    tap(w, f"prop:{w.p1.id}")
    tap(w, "d:other")
    assert say(w, "banana") == "date_bad"
    say(w, "25 Dec")
    assert w.repo.get_state(GUEST)["check_in"] == "2026-12-25"
    say(w, "3")  # typing a number at a list step works too
    assert w.repo.get_state(GUEST)["nights"] == 3


def test_parse_date():
    today = date(2026, 11, 2)
    assert parse_date("25 Dec", today) == date(2026, 12, 25)
    assert parse_date("Dec 25", today) == date(2026, 12, 25)
    assert parse_date("25/12", today) == date(2026, 12, 25)
    assert parse_date("1 Jan", today) == date(2027, 1, 1)
    assert parse_date("2026-12-25", today) == date(2026, 12, 25)
    assert parse_date("30 Feb", today) is None and parse_date("hello", today) is None


# --------------------------------------------------------------------------- languages and voice


def test_script_detection():
    assert detect_script("ഹായ്, മുറി വേണം") == "ml"
    assert detect_script("வணக்கம், அறை வேண்டும்") == "ta"
    assert detect_script("नमस्ते, कमरा चाहिए") == "hi"
    assert detect_script("hello there", "ta") == "ta"  # Latin keeps the stored language
    assert detect_script("hello there") == "en"


def test_every_string_exists_in_all_four_languages():
    for key, table in S.items():
        assert set(table) == {"en", "ml", "ta", "hi"}, key
        assert all(v.strip() for v in table.values()), key
    assert t("room_n", "xx", n=2) == "2 rooms"  # unknown language falls back to English


def test_first_message_in_malayalam_skips_the_picker(world):
    w = world
    assert say(w, "ഹായ്") == "menu"
    assert w.repo.get_state(GUEST)["lang"] == "ml"
    assert "മുറി" in w.wa.last()[2]["rows"][0][1]


def test_tamil_flow_labels(world):
    w = world
    say(w, "Hi")
    tap(w, "lang:ta")
    tap(w, "menu:book")
    tap(w, f"prop:{w.p1.id}")
    tap(w, "d:2026-11-10")
    assert w.wa.last()[2]["rows"][0][1] == "1 இரவு"


def test_voice_note_is_transcribed_and_used(world):
    w = world

    class Voice:
        def process(self, media_id):
            return type("R", (), {"text": "hello", "problem": None})()

    w.bot._voice_in = lambda: Voice()
    m = InboundMessage("wamid.voice", GUEST, "audio", datetime(2026, 11, 2, 4, 30), media_id="m1")
    assert w.bot.handle(m) == "language_picker"  # treated exactly like the typed text "hello"
    assert w.repo.list_chat(GUEST)[0].source == "voice"


def test_voice_problem_gets_text_reply(world):
    w = world
    w.bot._voice_in = lambda: None
    m = InboundMessage("wamid.voice2", GUEST, "audio", datetime(2026, 11, 2, 4, 30), media_id="m1")
    assert w.bot.handle(m) == "voice_problem"
    assert all(k in ("text",) for _, k, _ in w.wa.to(GUEST))  # never a voice reply


# --------------------------------------------------------------------------- AI, staff, opt-out


def test_free_question_goes_to_ai_with_property_facts(world):
    w = world
    say(w, "Hi")
    tap(w, "lang:en")
    tap(w, "menu:ask")
    w.llm.script.append(MockLLM.text("Check-in is from 14:00."))
    assert say(w, "What time is check-in?") == "answered"
    assert w.wa.texts()[-1] == "Check-in is from 14:00."
    system = w.llm.calls[0].system
    assert "14:00" in system and "Free cancellation 7 days" in system and "Deluxe" in system


def test_question_in_the_middle_of_booking_reprompts_the_step(world):
    w = world
    say(w, "Hi")
    tap(w, "lang:en")
    tap(w, "menu:book")
    tap(w, f"prop:{w.p1.id}")
    w.llm.script.append(MockLLM.text("Yes, pets are welcome."))
    say(w, "are pets allowed?")
    assert w.wa.texts()[-1] == "Yes, pets are welcome."
    assert w.wa.last()[1] == "list" and w.wa.last()[2]["rows"][-1][0] == "d:other"  # date list again


def test_ai_failure_falls_back_and_alerts_staff(world):
    w = world
    say(w, "Hi")
    tap(w, "lang:en")
    tap(w, "menu:ask")
    w.llm.script.append(LLMError("mock", "down"))
    say(w, "do you have a pool?")
    assert "could not answer" in w.wa.texts()[-1]


def test_staff_handoff_pauses_chat(world):
    w = world
    say(w, "Hi")
    tap(w, "lang:en")
    assert tap(w, "menu:staff") == "handoff"
    assert w.repo.is_paused(GUEST)[0] and "needs the team" in w.wa.texts(STAFF)[-1]
    before = len(w.wa.to(GUEST))
    assert say(w, "hello?") == "paused"
    assert len(w.wa.to(GUEST)) == before  # the bot stays quiet while staff own the chat
    w.clock.advance(hours=25)
    assert say(w, "hello again") != "paused"


def test_stop_and_start(world):
    w = world
    say(w, "Hi")
    tap(w, "lang:en")
    assert say(w, "STOP") == "opt_out"
    assert w.repo.get_guest(GUEST).offers_consent is False
    assert say(w, "START") == "opt_in"


# --------------------------------------------------------------------------- payments


def test_paid_event_confirms_booking_and_alerts_staff(world):
    w = world
    w.repo.touch_guest(GUEST, w.clock.now(), name="Asha")
    go_to_confirm(w, rooms=1)
    tap(w, "ok")
    b = w.repo.list_bookings(phone=GUEST)[0]
    assert w.bot.mark_paid(link_id=b.payment_link_id, payment_id="pay_1") == "paid"
    b = w.repo.get_booking(b.id)
    assert b.status == "paid" and b.razorpay_payment_id == "pay_1" and b.paid_at
    assert "confirmed" in w.wa.texts()[-1] and b.id in w.wa.texts()[-1]
    assert "PAID" in w.wa.texts(STAFF)[-1]
    assert w.bot.mark_paid(link_id=b.payment_link_id) == "already_paid"  # webhook retries are harmless


def test_failed_payment_message_has_retry_link_and_template_fallback(world):
    w = world
    w.repo.touch_guest(GUEST, w.clock.now(), name="Asha")
    go_to_confirm(w, rooms=1)
    tap(w, "ok")
    b = w.repo.list_bookings(phone=GUEST)[0]
    assert w.bot.payment_failed(booking_id=b.id) == "notified"
    assert "did not go through" in w.wa.texts()[-1] and b.payment_link_url in w.wa.texts()[-1]
    w.wa.text_error = WhatsAppError("outside window", status=400, code=131047)
    w.bot.config.templates.paid = "resort_paid"
    assert w.bot.mark_paid(booking_id=b.id) == "paid"
    assert w.wa.last()[1] == "template" and w.wa.last()[2]["name"] == "resort_paid"


def test_hold_expiry_sweep_releases_rooms_and_messages_guest(world):
    w = world
    w.repo.touch_guest(GUEST, w.clock.now(), name="Asha")
    go_to_confirm(w, rooms=2)
    tap(w, "ok")
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.clock.advance(minutes=11)
    assert w.bot.sweep() == {"expired": 0, "reminded": 1}  # nudge 4 minutes before the end
    assert "waiting for payment" in w.wa.texts()[-1]
    w.clock.advance(minutes=5)
    assert w.bot.sweep() == {"expired": 1, "reminded": 0}
    assert w.repo.get_booking(b.id).status == "expired" and "expired" in w.wa.texts()[-1]
    assert w.repo.free_rooms(w.deluxe, date(2026, 11, 10), date(2026, 11, 12), w.clock.now()) == 2


def test_paid_after_expiry_with_rooms_gone_needs_refund(world):
    w = world
    w.repo.touch_guest(GUEST, w.clock.now(), name="Asha")
    go_to_confirm(w, rooms=2)
    tap(w, "ok")
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.clock.advance(minutes=16)
    w.bot.sweep()
    other = Booking(w.p1.id, w.deluxe.id, "+919811111111", b.check_in, b.check_out, 2, 0, 2, 8000, 4000,
                    w.clock.now(), hold_expires_at=w.clock.now() + timedelta(minutes=15))
    w.repo.create_hold(other, w.clock.now())
    assert w.bot.mark_paid(booking_id=b.id) == "refund_needed"
    assert w.repo.get_booking(b.id).status == "expired" and "REFUND" in w.wa.texts(STAFF)[-1]


def _signed(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_razorpay_webhook_signature_and_paid(world):
    w = world
    w.repo.touch_guest(GUEST, w.clock.now(), name="Asha")
    go_to_confirm(w, rooms=1)
    tap(w, "ok")
    b = w.repo.list_bookings(phone=GUEST)[0]
    services = ResortServices(w.bot.config, w.repo, w.wa, w.pay, w.clock, None, _llm=w.llm)
    app = FastAPI()
    app.state.resort = services
    app.include_router(web.router)
    client = TestClient(app)
    payload = {"event": "payment_link.paid", "payload": {
        "payment_link": {"entity": {"id": b.payment_link_id, "reference_id": b.id}},
        "payment": {"entity": {"id": "pay_9", "amount": 400000}}}}
    body = json.dumps(payload).encode()
    assert client.post("/resort/razorpay/webhook", content=body, headers={"x-razorpay-signature": "bad"}).status_code == 400
    ok = client.post("/resort/razorpay/webhook", content=body, headers={"x-razorpay-signature": _signed(body, "whsec")})
    assert ok.status_code == 200
    assert w.repo.get_booking(b.id).status == "paid"
    assert verify_webhook_signature(body, _signed(body, "whsec"), "whsec")
    assert parse_event(payload).kind == "paid"


def test_meta_webhook_verify_and_signature(world, monkeypatch):
    monkeypatch.setenv("RESORT_WHATSAPP_VERIFY_TOKEN", "vt")
    monkeypatch.setenv("RESORT_WHATSAPP_APP_SECRET", "appsecret")
    monkeypatch.setenv("RESORT_WHATSAPP_PHONE_NUMBER_ID", "123")
    w = world
    services = ResortServices(w.bot.config, w.repo, w.wa, w.pay, w.clock, None, _llm=w.llm)
    app = FastAPI()
    app.state.resort = services
    app.include_router(web.router)
    client = TestClient(app)
    assert client.get("/resort/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt",
                                                 "hub.challenge": "42"}).text == "42"
    assert client.get("/resort/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no",
                                                 "hub.challenge": "42"}).status_code == 403
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "123"}, "contacts": [{"wa_id": "919847012345", "profile": {"name": "A"}}],
        "messages": [{"from": "919847012345", "id": "wamid.web1", "timestamp": "1790000000", "type": "text",
                      "text": {"body": "Hi"}}]}}]}]}
    body = json.dumps(payload).encode()
    assert client.post("/resort/webhook", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 403
    sig = "sha256=" + _signed(body, "appsecret")
    assert client.post("/resort/webhook", content=body, headers={"x-hub-signature-256": sig}).json() == {"received": 1}
    assert w.wa.last()[1] == "list"  # language picker sent


# --------------------------------------------------------------------------- WhatsApp client / parse


def test_interactive_reply_is_parsed_with_its_id():
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "123"},
        "messages": [{"from": "919847012345", "id": "w1", "timestamp": "1790000000", "type": "interactive",
                      "interactive": {"type": "list_reply", "list_reply": {"id": "rt:RT-1", "title": "Deluxe"}}},
                     {"from": "919847012345", "id": "w2", "timestamp": "1790000000", "type": "interactive",
                      "interactive": {"type": "button_reply", "button_reply": {"id": "ok", "title": "Confirm"}}},
                     {"from": "919847012345", "id": "w3", "timestamp": "1790000000", "type": "text",
                      "text": {"body": "hi"}}]}}]}]}
    got = parse_webhook(payload, "123")
    assert [(m.reply_id, m.text) for m in got] == [("rt:RT-1", "Deluxe"), ("ok", "Confirm"), (None, "hi")]


def test_client_sends_valid_interactive_payloads():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})

    wa = WhatsAppClient("1", "tok", http=httpx.Client(transport=httpx.MockTransport(handler)))
    wa.send_interactive_list(GUEST, "x" * 2000, "A very long button label", [("a", "t" * 40, "d" * 100)], header="H")
    inter = seen[0]["interactive"]
    assert inter["type"] == "list" and len(inter["body"]["text"]) == 1024
    assert len(inter["action"]["button"]) == 20
    row = inter["action"]["sections"][0]["rows"][0]
    assert len(row["title"]) == 24 and len(row["description"]) == 72
    wa.send_reply_buttons(GUEST, "pick", [("ok", "Confirm this booking please"), ("chg", "Change")])
    assert seen[1]["interactive"]["action"]["buttons"][0]["reply"]["title"] == "Confirm this booking pl"[:20]
    with pytest.raises(ValueError):
        wa.send_interactive_list(GUEST, "b", "go", [("a", "t", "")] * 11)
    with pytest.raises(ValueError):
        wa.send_reply_buttons(GUEST, "b", [("a", "t")] * 4)
