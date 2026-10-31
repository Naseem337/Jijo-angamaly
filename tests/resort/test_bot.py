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
from app.resort.flow import BookingBot
from app.resort.i18n import S, detect_script, t
from app.resort.models import Booking, Property, RateRule, RoomType
from app.resort.payments import MockPayments, PaymentError, parse_event, verify_webhook_signature
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
    p1 = repo.save_property(Property("Pozhiyoram Resort", city="Alleppey", place="Pozhiyoram, Alleppey", advance_pct=50,
                                     staff_numbers=[STAFF], policies="Free cancellation 7 days before.",
                                     contact_numbers=["+919497861924", "+919995855789"], extra_bed_rate=500,
                                     map_url="https://maps.app.goo.gl/abc", highlights=["Beach view from every room"]))
    p2 = repo.save_property(Property("Vibes Kanthalloor", city="Munnar", place="Kanthalloor, Munnar", advance_pct=50))
    deluxe = repo.save_room_type(RoomType(p1.id, "Deluxe", units=2, max_adults=2, max_children=1, max_occupancy=3,
                                          base_rate=4000))
    family = repo.save_room_type(RoomType(p1.id, "Family Suite", units=3, max_adults=4, max_children=2,
                                          max_occupancy=6, base_rate=8000))
    repo.save_room_type(RoomType(p2.id, "Cottage", units=2, base_rate=5000))
    clock = FixedClock(datetime(2026, 11, 2, 10, 0, tzinfo=IST))  # a Monday
    wa, pay = FakeWA(), MockPayments()
    llm = MockLLM()
    bot = BookingBot(load_resort_config(), repo, wa, pay, clock, llm=lambda: llm)
    return type("W", (), dict(repo=repo, wa=wa, pay=pay, clock=clock, bot=bot, llm=llm, p1=p1, p2=p2, deluxe=deluxe,
                              family=family))()


def msg(phone, text="", rid=None, kind=None):
    return InboundMessage(f"wamid.{next(_ids)}", phone, kind or ("interactive" if rid else "text"),
                          datetime(2026, 11, 2, 4, 30), text=text or (rid or ""), reply_id=rid)


def say(w, text, phone=GUEST, *script):
    """The guest writes `text`; `script` is what the (mock) AI does: tool calls and the final reply."""
    w.llm.script.extend(script)
    return w.bot.handle(msg(phone, text))


tool, reply = MockLLM.tool, MockLLM.text


def last_tool(w) -> dict:
    """The newest tool result the AI was shown."""
    return json.loads([m for m in w.llm.calls[-1].messages if m.role == "tool"][-1].content)


def to_summary(w, *, place="Alleppey", adults=2, nights=2, rt="Deluxe", rooms=1, beds=0, check_in="2026-11-10",
               phone=GUEST):
    say(w, f"room for {adults} on 10 Nov, {nights} nights", phone,
        tool("check_availability", place=place, check_in=check_in, nights=nights, adults=adults, language="en"),
        reply("We have Deluxe and Family Suite. Which one?"))
    say(w, f"{rt} please", phone,
        tool("prepare_booking", room_type=rt, rooms=rooms, extra_beds=beds),
        reply("Summary ... shall I confirm?"))


def confirm(w, phone=GUEST):
    return say(w, "yes confirm, Asha Menon", phone, tool("create_booking", guest_name="Asha Menon"),
               reply("Done, your rooms are held."))


def hold(w, rooms=1, nights=2, rt="Deluxe", phone=GUEST):
    """A guest chats all the way to a held booking with a payment link."""
    to_summary(w, rt=rt, rooms=rooms, nights=nights, adults=2 * rooms, phone=phone)
    return confirm(w, phone)


# --------------------------------------------------------------------------- the conversation


def test_hi_asks_where_with_no_menu_and_no_ai_call(world):
    w = world
    assert say(w, "hii") == "greeting"
    assert w.wa.last() == (GUEST, "text",
                           'Hi! Where are you looking to stay: "Pozhiyoram, Alleppey" or "Kanthalloor, Munnar"?')
    assert [k for _, k, _ in w.wa.to(GUEST)] == ["text"] and w.llm.calls == []


def test_greeting_in_other_scripts_answers_in_that_language(world):
    w = world
    say(w, "ഹായ്")
    assert "എവിടെ" in w.wa.texts()[-1] and w.repo.get_state(GUEST)["lang"] == "ml"
    say(w, "नमस्ते", "+919847000099")
    assert "कहाँ" in w.wa.texts("+919847000099")[-1]


def test_booking_chat_end_to_end_with_payment_link(world):
    w = world
    say(w, "Alleppey, 10 Nov 2 nights, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=2, adults=2, language="en"),
        reply("Deluxe is Rs 4,000 a night, Family Suite Rs 8,000. Which one?"))
    res = last_tool(w)
    assert res["place"] == "Pozhiyoram, Alleppey" and res["check_out"] == "2026-11-12"
    deluxe = next(o for o in res["options"] if o["room_type"] == "Deluxe")
    assert deluxe["total_for_stay"] == 8000 and deluxe["advance_now"] == 4000 and deluxe["rooms_possible"] == [1, 2]
    assert w.wa.texts()[-1].startswith("Deluxe is Rs 4,000")
    say(w, "deluxe, 1 room", GUEST, tool("prepare_booking", room_type="deluxe", rooms=1), reply("Summary. Confirm?"))
    summary = last_tool(w)
    assert (summary["total"], summary["advance_now"], summary["balance_at_property"]) == (8000, 4000, 4000)
    assert summary["guest_name_known"] is False and w.repo.list_bookings(phone=GUEST) == []  # nothing booked yet
    assert confirm(w) == "payment_link"
    booking = w.repo.list_bookings(phone=GUEST)[0]
    assert (booking.status, booking.advance, booking.total, booking.rooms) == ("hold", 4000, 8000, 1)
    assert booking.guest_name == "Asha Menon" and w.pay.created[0]["amount"] == 4000
    texts = w.wa.texts()
    assert texts[-2] == "Done, your rooms are held." and "https://rzp.io/mock/" in texts[-1] and "4,000" in texts[-1]
    assert {k for _, k, _ in w.wa.to(GUEST)} == {"text"}  # never a list or button
    assert "bk" not in w.repo.get_state(GUEST)


def test_system_prompt_has_the_rules_and_facts(world):
    w = world
    say(w, "do you allow pets?", GUEST, reply("Sorry, I am not sure about pets."))
    system = w.llm.calls[0].system
    assert "9497861924" in system and "9995855789" in system and "https://maps.app.goo.gl/abc" in system
    assert "Never ask which language" in system and "Manglish" in system and "Hinglish" in system
    assert "call the owner" in system and "NEGOTIATION" in system and "Never offer a" in system
    assert "₹4,000" in system and "Extra bed: ₹500" in system
    assert "Kanthalloor, Munnar" in system and "2026-11-02 (Monday)" in system


def test_booking_cannot_be_created_in_the_same_turn_as_the_summary(world):
    w = world
    say(w, "book deluxe 10 Nov 2 nights for 2", GUEST,
        tool("check_availability", place="alleppey", check_in="2026-11-10", nights=2, adults=2),
        tool("prepare_booking", room_type="Deluxe", rooms=1),
        tool("create_booking", guest_name="Asha"),
        reply("Please confirm the summary."))
    assert "has not seen the summary" in last_tool(w)["error"]
    assert w.repo.list_bookings(phone=GUEST) == [] and w.pay.created == []


def test_create_needs_a_prepared_booking_and_a_name(world):
    w = world
    say(w, "yes book it", GUEST, tool("create_booking", guest_name="Asha"), reply("Which dates?"))
    assert "no prepared booking" in last_tool(w)["error"]
    to_summary(w)
    say(w, "yes", GUEST, tool("create_booking"), reply("May I have your name?"))
    assert "name" in last_tool(w)["error"] and w.repo.list_bookings(phone=GUEST) == []


def test_ten_guests_never_one_room(world):
    w = world
    say(w, "10 of us, Alleppey 10 Nov 1 night", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=1, adults=10), reply("ok"))
    options = last_tool(w)["options"]
    # Family Suite fits 4 adults: 10 people need 3 rooms (3 free), never 1 or 2. Deluxe can't take them.
    assert [(o["room_type"], o["rooms_possible"]) for o in options] == [("Family Suite", [3])]
    say(w, "1 room", GUEST, tool("prepare_booking", room_type="Family Suite", rooms=1), reply("not possible"))
    assert "not possible" in last_tool(w)["error"]


def test_no_room_fits_says_so(world):
    w = world
    say(w, "20 people", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=1, adults=20), reply("Sorry"))
    res = last_tool(w)
    assert res["available"] is False and res["options"] == []


def test_dates_in_the_past_are_refused(world):
    w = world
    say(w, "yesterday", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-01", nights=1, adults=2), reply("Which date?"))
    assert "past" in last_tool(w)["error"]


def test_season_rates_and_extra_bed(world):
    w = world
    w.repo.save_rate_rule(RateRule(w.deluxe.id, date(2026, 11, 11), date(2026, 11, 20), 5000, "season"))
    say(w, "3 nights from 10 Nov", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=3, adults=2), reply("ok"))
    deluxe = next(o for o in last_tool(w)["options"] if o["room_type"] == "Deluxe")
    assert deluxe["rate_per_room_per_night"] == "1 night x Rs 4,000, 2 nights x Rs 5,000"
    assert deluxe["total_for_stay"] == 14000
    say(w, "deluxe with one extra bed", GUEST, tool("prepare_booking", room_type="Deluxe", rooms=1, extra_beds=1),
        reply("summary"))
    summary = last_tool(w)
    assert summary["extra_bed_cost"] == 1500 and summary["total"] == 15500 and summary["advance_now"] == 7750
    assert w.repo.get_state(GUEST)["bk"]["chosen"]["total"] == 15500


def test_extra_beds_refused_where_not_offered(world):
    w = world
    to_summary(w, place="Munnar", rt="Cottage", adults=2)
    assert last_tool(w)["room_type"] == "Cottage"
    say(w, "add 1 extra bed", GUEST, tool("prepare_booking", room_type="Cottage", rooms=1, extra_beds=1), reply("no"))
    assert "extra beds are not available" in last_tool(w)["error"]


def test_place_is_matched_from_words_the_guest_uses(world):
    w = world
    say(w, "munnar", GUEST,
        tool("check_availability", place="Munnar", check_in="2026-11-10", nights=1, adults=2), reply("ok"))
    assert last_tool(w)["place"] == "Kanthalloor, Munnar"
    say(w, "goa", GUEST,
        tool("check_availability", place="Goa", check_in="2026-11-10", nights=1, adults=2), reply("ok"))
    assert last_tool(w)["error"] == "unknown place" and "Pozhiyoram, Alleppey" in last_tool(w)["places"]


def test_language_style_is_remembered_for_fixed_messages(world):
    w = world
    say(w, "Alleppey il 10 nu room venam", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=1, adults=2, language="manglish"),
        reply("Deluxe 4000 aanu."))
    assert w.repo.get_state(GUEST)["lang"] == "mn" and w.repo.get_state(GUEST)["style"] == "manglish"
    say(w, "இரண்டு பேர்", GUEST, reply("சரி"))
    assert w.repo.get_state(GUEST)["lang"] == "ta"


def test_history_reaches_the_ai_in_order(world):
    w = world
    say(w, "do you have AC?", GUEST, reply("Yes, all rooms are AC."))
    say(w, "and wifi?", GUEST, reply("Not sure."))
    roles = [(m.role, m.content) for m in w.llm.calls[-1].messages]
    assert roles == [("user", "do you have AC?"), ("assistant", "Yes, all rooms are AC."), ("user", "and wifi?")]


def test_my_bookings_tool(world):
    w = world
    hold(w, rooms=1)
    say(w, "what is my booking status?", GUEST, tool("my_bookings"), reply("It is on hold."))
    row = last_tool(w)["bookings"][0]
    assert row["status"] == "on hold" and row["total"] == 8000 and row["place"] == "Pozhiyoram, Alleppey"
    assert row["payment_link"].startswith("https://rzp.io/mock/")


def test_soldout_race_is_reported_to_the_ai(world):
    w = world
    to_summary(w, rooms=2)
    other = Booking(w.p1.id, w.deluxe.id, "+919811111111", date(2026, 11, 10), date(2026, 11, 12), 2, 0, 2, 8000,
                    4000, w.clock.now(), hold_expires_at=w.clock.now() + timedelta(minutes=15))
    w.repo.create_hold(other, w.clock.now())  # someone else grabbed both Deluxe rooms first
    say(w, "yes, Asha", GUEST, tool("create_booking", guest_name="Asha"), reply("Sorry, those rooms just went."))
    assert "just taken" in last_tool(w)["error"] and w.repo.list_bookings(phone=GUEST) == []
    assert w.pay.created == []


def test_stale_button_tap_is_ignored(world):
    w = world
    assert w.bot.handle(msg(GUEST, rid="menu:book")) == "ignored"
    assert w.llm.calls == [] and w.wa.sent == []


def test_ai_failure_apologises_and_alerts_staff(world):
    w = world
    say(w, "do you have a pool?", GUEST, LLMError("mock", "down"))
    assert "could not answer" in w.wa.texts()[-1] and "could not answer a message" in w.wa.texts(STAFF)[-1]


def test_payment_link_failure_hands_off(world, monkeypatch):
    w = world
    to_summary(w)

    def boom(*a, **k):
        raise PaymentError("razorpay down")

    monkeypatch.setattr(w.pay, "create_payment_link", boom)
    say(w, "yes, Asha", GUEST, tool("create_booking", guest_name="Asha"), reply("never shown"))
    assert w.repo.is_paused(GUEST)[0] and "payment link" in w.wa.texts(STAFF)[-1]
    assert "payment link" in w.wa.texts()[-1] and "never shown" not in " ".join(w.wa.texts())
    assert w.repo.list_bookings(phone=GUEST)[0].status == "failed"


# --------------------------------------------------------------------------- languages and voice


def test_script_detection():
    assert detect_script("ഹായ്, മുറി വേണം") == "ml"
    assert detect_script("வணக்கம், அறை வேண்டும்") == "ta"
    assert detect_script("नमस्ते, कमरा चाहिए") == "hi"
    assert detect_script("hello there", "ta") == "ta"  # Latin keeps the stored language
    assert detect_script("hello there") == "en"


def test_every_string_exists_in_all_four_languages():
    for key, table in S.items():
        assert {"en", "ml", "ta", "hi"} <= set(table) <= {"en", "ml", "ta", "hi", "mn"}, key
        assert all(v.strip() for v in table.values()), key
    assert t("greet_ask", "xx", hi="Hi", places="x") == "Hi! Where are you looking to stay: x?"  # unknown language: English


def test_voice_note_is_transcribed_and_used(world):
    w = world

    class Voice:
        def process(self, media_id):
            return type("R", (), {"text": "hello", "problem": None})()

    w.bot._voice_in = lambda: Voice()
    m = InboundMessage("wamid.voice", GUEST, "audio", datetime(2026, 11, 2, 4, 30), media_id="m1")
    assert w.bot.handle(m) == "greeting"  # treated exactly like the typed text "hello"
    assert w.repo.list_chat(GUEST)[0].source == "voice"


def test_voice_problem_gets_text_reply(world):
    w = world
    w.bot._voice_in = lambda: None
    m = InboundMessage("wamid.voice2", GUEST, "audio", datetime(2026, 11, 2, 4, 30), media_id="m1")
    assert w.bot.handle(m) == "voice_problem"
    assert all(k in ("text",) for _, k, _ in w.wa.to(GUEST))  # never a voice reply


# --------------------------------------------------------------------------- staff, opt-out


def test_staff_handoff_pauses_chat(world):
    w = world
    w.bot.handoff(GUEST, {"lang": "en"}, "guest asked for the team")
    assert w.repo.is_paused(GUEST)[0] and "needs the team" in w.wa.texts(STAFF)[-1]
    before = len(w.wa.to(GUEST))
    assert say(w, "hello?") == "paused"
    assert len(w.wa.to(GUEST)) == before  # the bot stays quiet while staff own the chat
    w.clock.advance(hours=25)
    assert say(w, "hello again", GUEST, reply("Hi, how can I help?")) != "paused"


def test_stop_and_start(world):
    w = world
    assert say(w, "STOP") == "opt_out"
    assert w.repo.get_guest(GUEST).offers_consent is False
    assert say(w, "START") == "opt_in"


def test_paid_message_carries_the_map_link(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.bot.mark_paid(booking_id=b.id)
    paid = next(x for x in w.wa.texts() if "confirmed" in x)
    assert "Location: https://maps.app.goo.gl/abc" in paid


# --------------------------------------------------------------------------- payments

def test_paid_event_confirms_booking_and_alerts_staff(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    assert w.bot.mark_paid(link_id=b.payment_link_id, payment_id="pay_1") == "paid"
    b = w.repo.get_booking(b.id)
    assert b.status == "paid" and b.razorpay_payment_id == "pay_1" and b.paid_at
    confirmed = next(x for x in w.wa.texts() if "confirmed" in x)
    assert b.id in confirmed and "ID proof" in w.wa.texts()[-1]  # the ID request follows in plain chat
    assert "PAID" in w.wa.texts(STAFF)[-1]
    assert w.bot.mark_paid(link_id=b.payment_link_id) == "already_paid"  # webhook retries are harmless


def test_failed_payment_message_has_retry_link_and_template_fallback(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    assert w.bot.payment_failed(booking_id=b.id) == "notified"
    assert "didn't go through" in w.wa.texts()[-1] and b.payment_link_url in w.wa.texts()[-1]
    w.wa.text_error = WhatsAppError("outside window", status=400, code=131047)
    w.bot.config.templates.paid = "resort_paid"
    assert w.bot.mark_paid(booking_id=b.id) == "paid"
    assert w.wa.last()[1] == "template" and w.wa.last()[2]["name"] == "resort_paid"


def test_hold_expiry_sweep_releases_rooms_and_messages_guest(world):
    w = world
    hold(w, rooms=2)
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.clock.advance(minutes=11)
    assert w.bot.sweep() == {"expired": 0, "reminded": 1}  # nudge 4 minutes before the end
    assert "still open for 4 more minutes" in w.wa.texts()[-1] and "Booking RB-" not in w.wa.texts()[-1]
    w.clock.advance(minutes=5)
    texts_before = list(w.wa.texts())
    assert w.bot.sweep() == {"expired": 1, "reminded": 0}
    assert w.repo.get_booking(b.id).status == "expired" and w.wa.texts() == texts_before  # released quietly, no message
    assert w.repo.free_rooms(w.deluxe, date(2026, 11, 10), date(2026, 11, 12), w.clock.now()) == 2


def test_paid_after_expiry_with_rooms_gone_needs_refund(world):
    w = world
    hold(w, rooms=2)
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
    hold(w, rooms=1)
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
    assert w.wa.last()[1] == "text" and "Where are you looking to stay" in w.wa.last()[2]


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


# --------------------------------------------------------------------------- payment help in chat


def test_resend_payment_link_while_on_hold(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.clock.advance(minutes=5)
    assert say(w, "link not opening, send again", GUEST, tool("send_payment_link"),
               reply("Sure, sending it again.")) == "payment_link"
    last = w.wa.texts()[-1]
    assert b.payment_link_url in last and "10 minutes" in last and "UPI" in last


def test_resend_refused_after_the_hold_ran_out(world):
    w = world
    hold(w, rooms=1)
    w.clock.advance(minutes=16)
    say(w, "send the link", GUEST, tool("send_payment_link"), reply("That booking expired, shall we book again?"))
    assert "no booking is waiting for payment" in last_tool(w)["error"]
    assert "https://rzp.io" not in w.wa.texts()[-1]


def test_system_prompt_explains_payment_methods_and_stuck_payments(world):
    w = world
    say(w, "can I pay by gpay?", GUEST, reply("Yes, UPI works."))
    system = w.llm.calls[0].system
    assert "UPI (GPay, PhonePe, Paytm" in system and "money was deducted" in system and "Never promise a refund" in system


def test_razorpay_link_takes_all_methods_and_returns_the_guest_to_whatsapp():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/abc"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    from app.resort.payments import RazorpayPayments
    link = RazorpayPayments("k", "s", http=http, callback_url="https://wa.me/919800000000").create_payment_link(
        4000, "RB-1", "Asha", GUEST, "Stay", 1790000000)
    assert link.url == "https://rzp.io/i/abc"
    body = seen[0]
    assert body["amount"] == 400000 and body["callback_url"] == "https://wa.me/919800000000"
    assert body["callback_method"] == "get" and "upi_link" not in body  # not UPI-only: cards and net banking too


# --------------------------------------------------------------------------- leads saved from the chat


def test_the_chat_saves_a_lead_at_every_step(world):
    from app.resort import leads
    w = world
    say(w, "Alleppey 10 Nov 2 nights for 2", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=2, adults=2, rooms=1),
        reply("Deluxe or Family Suite?"))
    lead = leads.get_lead(w.repo, GUEST)
    assert (lead["stage"], lead["check_in"], lead["check_out"], lead["adults"], lead["rooms"]) == (
        "asked", "2026-11-10", "2026-11-12", 2, 1)
    assert lead["property_id"] == w.p1.id and lead["booking_id"] == ""
    say(w, "deluxe", GUEST, tool("prepare_booking", room_type="Deluxe", rooms=1), reply("Summary. Confirm?"))
    assert leads.get_lead(w.repo, GUEST)["stage"] == "summary" and leads.get_lead(w.repo, GUEST)["room_type"] == "Deluxe"
    say(w, "yes, Asha Menon from Kochi", GUEST, tool("create_booking", guest_name="Asha Menon", from_city="Kochi"),
        reply("Held."))
    booking = w.repo.list_bookings(phone=GUEST)[0]
    lead = leads.get_lead(w.repo, GUEST)
    assert (lead["stage"], lead["booking_id"], lead["from_city"]) == ("held", booking.id, "Kochi")
    say(w, "another stay 1 Dec 1 night", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-12-01", nights=1, adults=2), reply("ok"))
    lead = leads.get_lead(w.repo, GUEST)
    assert (lead["stage"], lead["booking_id"], lead["check_in"], lead["from_city"]) == ("asked", "", "2026-12-01", "Kochi")


# --------------------------------------------------------------------------- tester feedback: language, flow, payment wording


def test_manglish_guest_gets_manglish_fixed_messages_and_no_booked_before_payment(world):
    w = world
    say(w, "Alleppey il room undo, 10 nu 2 divasam, 2 per", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=2, adults=2, language="manglish"),
        reply("Und bro. Deluxe 4000, Family Suite 8000. Ethu mathi?"))
    say(w, "deluxe mathi", GUEST, tool("prepare_booking", room_type="Deluxe", rooms=1, language="manglish"), reply("Summary"))
    say(w, "ok confirm, Asha", GUEST, tool("create_booking", guest_name="Asha", language="manglish"),
        reply("Payment complete cheythaal booking confirm aakum."))
    pay_text = w.wa.texts()[-1]
    assert "payment complete cheyyuka" in pay_text and "UPI" in pay_text and w.repo.get_state(GUEST)["lang"] == "mn"
    for banned in ("held", "booked", "reserved"):
        assert banned not in pay_text.lower()
    assert "Booking RB-" not in w.wa.texts()[-2]  # the AI's own line does not claim a confirmed booking either


def test_english_payment_message_asks_to_complete_payment_not_to_trust_a_hold(world):
    w = world
    hold(w, rooms=1)
    text = w.wa.texts()[-1]
    assert "complete the payment" in text and "confirm your booking" in text
    for banned in ("held", "is booked", "reserved"):
        assert banned not in text.lower()


def test_hi_for_hi_and_it_asks_if_they_are_looking_for_a_stay_when_only_one_place_is_open(world):
    w = world
    w.repo.save_property(Property(**{**w.p2.__dict__, "active": False}))  # Kanthalloor switched off
    assert say(w, "hi") == "greeting"
    assert w.wa.texts()[-1] == "Hi! Are you looking for a stay at Pozhiyoram, Alleppey?"
    say(w, "Hello", "+919847000011")
    assert w.wa.texts("+919847000011")[-1].startswith("Hello! Are you looking")
    w.repo.set_state("+919847000012", {"lang": "mn"})
    say(w, "hi", "+919847000012")
    assert w.wa.texts("+919847000012")[-1] == "Hi! Pozhiyoram, Alleppey-il stay aanu nokkunnath alle?"
    assert w.llm.calls == []  # greetings never cost an AI call


def test_manglish_guest_never_gets_malayalam_script(world):
    w = world
    say(w, "avde engane und", GUEST, reply("അവിടെ നല്ലതാണ്, ബീച്ച് ഉണ്ട്"), reply("Avde nalla rasam und bro, beach um und."))
    assert w.wa.texts() == ["Avde nalla rasam und bro, beach um und."]
    assert "Rewrite that reply using only English letters" in w.llm.calls[1].messages[-1].content


def test_malayalam_script_guest_is_not_rewritten(world):
    w = world
    say(w, "അവിടെ എങ്ങനെയുണ്ട്", GUEST, reply("അവിടെ നല്ലതാണ്, ബീച്ച് ഉണ്ട്"))
    assert w.wa.texts() == ["അവിടെ നല്ലതാണ്, ബീച്ച് ഉണ്ട്"] and len(w.llm.calls) == 1


def test_ok_after_a_statement_gets_no_reply_but_ok_after_a_question_does(world):
    w = world
    hold(w, rooms=1)  # the last message to the guest is the payment link: nothing is being asked
    before = len(w.wa.to(GUEST))
    calls = len(w.llm.calls)
    for ack in ("ok", "Okay thanks", "👍", "sheri", "ശരി"):
        assert say(w, ack) == "quiet", ack
    assert len(w.wa.to(GUEST)) == before and len(w.llm.calls) == calls  # no spam, no AI cost
    say(w, "any pool nearby?", GUEST, reply("Not sure about that. Want me to check?"))  # a question from us
    assert say(w, "ok", GUEST, reply("Sure, I will ask the owner.")) == "answered"


def test_ok_is_an_answer_while_a_booking_summary_waits_for_confirmation(world):
    w = world
    to_summary(w)
    assert say(w, "ok", GUEST, tool("create_booking", guest_name="Asha"), reply("Please complete the payment.")) == "payment_link"


def test_the_ai_can_choose_to_stay_quiet(world):
    w = world
    say(w, "what a lovely place, thanks for the info", GUEST, reply("Glad you like it?"))
    before = len(w.wa.to(GUEST))
    assert say(w, "will tell my wife", GUEST, reply("[no reply]")) == "quiet"
    assert len(w.wa.to(GUEST)) == before


def test_a_quick_second_message_is_answered_once_with_both_in_view(world, monkeypatch):
    from app.resort import chat
    w = world
    w.bot.debounce_s = 1.0

    def second_message_arrives(seconds):
        w.bot.debounce_s = 0
        say(w, "Alleppey", GUEST, reply("Avde nalla rasam bro, beach view und."))

    monkeypatch.setattr(chat.time, "sleep", second_message_arrives)
    assert say(w, "avde engane und") == "superseded"  # the newer message answers
    assert w.wa.texts() == ["Avde nalla rasam bro, beach view und."]
    assert w.llm.calls[0].messages[-1].content == "avde engane und\nAlleppey"


def test_the_prompt_carries_the_language_summary_and_payment_rules(world):
    w = world
    say(w, "hello there", GUEST, reply("Hi, what are you looking for?"))
    system = w.llm.calls[0].system
    assert "ONE language per reply" in system and "NEVER in Malayalam script" in system
    assert "always write it in English" in system
    assert "NEVER say the room or booking is booked, confirmed, reserved or done" in system
    assert "[no reply]" in system and "belong together" in system


def test_missed_webhook_is_caught_by_asking_razorpay(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    assert w.bot.sweep().get("paid_found") is None  # nothing paid yet
    w.pay.paid_links[b.payment_link_id] = "pay_77"
    assert w.bot.sweep()["paid_found"] == 1
    paid = w.repo.get_booking(b.id)
    assert paid.status == "paid" and paid.razorpay_payment_id == "pay_77"
    assert any("confirmed" in text for text in w.wa.texts())
    assert "paid_found" not in w.bot.sweep()  # only once


def test_guest_saying_i_paid_is_confirmed_through_my_bookings(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.pay.paid_links[b.payment_link_id] = "pay_88"
    say(w, "paid bro", GUEST, tool("my_bookings"), reply("[no reply]"))
    assert w.repo.get_booking(b.id).status == "paid"
    assert "do not repeat" in last_tool(w)["note"] and last_tool(w)["bookings"][0]["status"] == "confirmed"


def test_razorpay_link_status_reads_paid_and_the_payment_id():
    from app.resort.payments import RazorpayPayments

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/payment_links/plink_1")
        return httpx.Response(200, json={"id": "plink_1", "status": "paid", "payments": [{"payment_id": "pay_9"}]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert RazorpayPayments("k", "s", http=http).link_status("plink_1") == {"paid": True, "payment_id": "pay_9", "failed_payment_id": ""}


def test_manglish_texts_keep_the_placeholders_of_the_english_ones():
    import string
    for key, table in S.items():
        if "mn" in table:
            names = lambda text: {f for _, f, _, _ in string.Formatter().parse(text) if f}  # noqa: E731
            assert names(table["mn"]) <= names(table["en"]), key


def test_no_book_again_template_is_left_in_the_guest_messages():
    assert "expired" not in S  # a hold that runs out is released without a message
    for key, table in S.items():
        for lang, text in table.items():
            assert "Send Hi" not in text and "Hi അയക്കൂ" not in text, (key, lang)


# --------------------------------------------------------------------------- the guest is told when a payment fails


def test_a_failed_attempt_is_found_and_the_guest_is_told_once_without_any_webhook(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    before = len(w.wa.texts())
    w.pay.failed_links[b.payment_link_id] = "pay_bad1"
    assert w.bot.sweep() == {"expired": 0, "reminded": 0}
    told = w.wa.texts()[before:]
    assert len(told) == 1 and "didn't go through" in told[0] and b.payment_link_url in told[0]
    assert w.repo.get_booking(b.id).status == "hold"  # still held: they can try again with the same link
    w.bot.sweep()
    assert len(w.wa.texts()) == before + 1  # not told again for the same attempt
    w.pay.failed_links[b.payment_link_id] = "pay_bad2"  # a second failed attempt is news again
    w.bot.sweep()
    assert len(w.wa.texts()) == before + 2


def test_webhook_and_our_own_check_do_not_both_tell_the_guest(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    before = len(w.wa.texts())
    assert w.bot.payment_failed(booking_id=b.id, payment_id="pay_bad1") == "notified"  # the webhook arrives first
    w.pay.failed_links[b.payment_link_id] = "pay_bad1"
    w.bot.sweep()  # our own check sees the same attempt
    assert len(w.wa.texts()) == before + 1


def test_a_failed_attempt_followed_by_a_good_payment_only_confirms(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    before = len(w.wa.texts())
    w.pay.failed_links[b.payment_link_id] = "pay_bad1"
    w.pay.paid_links[b.payment_link_id] = "pay_ok"
    w.bot.sweep()
    new = w.wa.texts()[before:]
    assert w.repo.get_booking(b.id).status == "paid" and any("confirmed" in t for t in new)
    assert not any("didn't go through" in t for t in new)


def test_razorpay_link_status_finds_a_failed_attempt_through_the_order():
    from app.resort.payments import RazorpayPayments

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/payment_links/plink_1"):
            return httpx.Response(200, json={"id": "plink_1", "status": "created", "order_id": "order_1", "payments": []})
        assert request.url.path.endswith("/orders/order_1/payments")
        return httpx.Response(200, json={"items": [{"id": "pay_a", "status": "failed"}, {"id": "pay_b", "status": "failed"}]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert RazorpayPayments("k", "s", http=http).link_status("plink_1") == {
        "paid": False, "payment_id": "", "failed_payment_id": "pay_b"}
