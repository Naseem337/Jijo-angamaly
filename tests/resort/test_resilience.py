"""Failures, limits and odd input for the resort WhatsApp bot (pre-launch, 2026-11-19).

The AI, WhatsApp, Razorpay and speech-to-text can all fail; guests send anything. The bot must never crash, never
go silent where a reply is due, never spam the staff, and never burn unlimited AI calls."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
from test_bot import (GUEST, STAFF, InboundMessage, _ids, hold, reply, say, to_summary,  # noqa: F401
                      tool, world)

from app.providers.llm import LLMError, LLMResponse, MockLLM, ToolCall
from app.resort import flow
from app.resort.models import Booking, Property
from app.resort.payments import PaymentError
from app.whatsapp.client import WhatsAppError


def msg(text="", kind="text", phone=GUEST, **kw):
    return InboundMessage(f"wamid.r{next(_ids)}", phone, kind, datetime(2026, 11, 2, 4, 30), text=text, **kw)


class DownLLM:
    name = "down"

    def __init__(self):
        self.calls = 0

    def complete(self, *a, **k):
        self.calls += 1
        raise LLMError("down", "all providers failed (timeout, 429, 503)")


# --------------------------------------------------------------------------- 1. AI provider failures


def test_ai_down_apologises_and_alerts_the_staff_once_not_per_message(world):
    w = world
    w.bot._llm = lambda: DownLLM()
    for text in ("is there parking?", "pool undo?", "wifi?"):
        w.bot.handle(msg(text))
    assert w.wa.texts()[0].startswith("Sorry")  # the fixed apology
    alerts = [x for x in w.wa.texts(STAFF) if "could not answer" in x]
    assert len(alerts) == 1  # one alert for the outage, not one per guest message


def test_empty_ai_answer_gets_the_apology_never_silence(world):
    w = world
    assert say(w, "is there parking?", GUEST, reply("")) == "ai_down"
    assert w.wa.texts()[-1].startswith("Sorry")


def test_a_tool_loop_that_never_ends_stops_and_the_guest_is_answered(world):
    w = world
    loop = [tool("my_bookings") for _ in range(12)]
    out = say(w, "can you check something for me", GUEST, *loop)
    assert out in ("ai_down", "answered") and w.wa.texts()  # bounded by MAX_TOOL_ROUNDS, never hangs or crashes
    w.llm.script.clear()


def test_unknown_tool_and_malformed_arguments_do_not_crash(world):
    w = world
    bad = LLMResponse("", [ToolCall("c1", "delete_everything", {}),
                           ToolCall("c2", "check_availability", {}, arguments_error="invalid JSON")],
                      "tool_calls", provider="mock")
    w.llm.script.extend([bad, reply("Which dates?")])
    assert w.bot.handle(msg("is it near the beach?")) == "answered"
    results = [m.content for m in w.llm.calls[-1].messages if m.role == "tool"]
    assert any("unknown tool" in r for r in results) and any("invalid JSON" in r for r in results)


# --------------------------------------------------------------------------- 2. WhatsApp failures


def test_a_network_error_while_sending_never_crashes_and_the_retry_is_not_swallowed(world):
    w = world
    sent: list[str] = []

    def flaky(to, text):
        if not sent:
            sent.append("failed once")
            raise httpx.ConnectError("network down")
        w.wa.sent.append((to, "text", text))
        return "wamid.ok"

    w.wa.send_text = flaky
    assert w.bot.handle(msg("hi")) == "greeting"  # no exception reaches the webhook worker
    assert w.wa.texts() == []
    w.wa.send_text = type(w.wa).send_text.__get__(w.wa)
    assert w.bot.handle(msg("hello")) == "greeting"
    assert w.wa.texts() == ["Hi, How may I assist you?"]  # the failed one was not logged as sent


@pytest.mark.parametrize("code", [131047, 131056, 500])
def test_whatsapp_errors_are_logged_not_raised(world, code):
    w = world
    w.wa.text_error = WhatsAppError(f"error {code}", code=code)
    assert w.bot.handle(msg("hi")) == "greeting"
    assert w.repo.list_chat(GUEST)[-1].direction == "in"  # nothing recorded as sent


def test_photo_upload_failure_does_not_stop_the_chat(world, tmp_path, monkeypatch):
    from app.resort import photos as ph
    monkeypatch.setenv("RESORT_DB_PATH", str(tmp_path / "r.sqlite3"))
    monkeypatch.setattr(ph, "_shrink", lambda data: None)
    w = world
    refs = [ph.save_photo(b"\x89PNG\r\n\x1a\n" + b"x" * 40) for _ in range(2)]
    w.repo.save_property(Property(**{**w.repo.get_property(w.p1.id).__dict__, "photo_urls": refs,
                                     "intro": "Glimpse {price}"}))

    def boom(*a, **k):
        raise WhatsAppError("upload failed", code=500)

    w.wa.upload_media = boom
    say(w, "Alleppey, 10 nov to 12 nov, 2 people", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", check_out="2026-11-12", adults=2),
        reply("[no reply]"))
    texts = w.wa.texts()
    assert texts[0].startswith("Yes, rooms are available") and texts[-1].startswith("Please share your name")


# --------------------------------------------------------------------------- 3. payments


def test_checking_payments_when_razorpay_is_down_never_crashes(world, monkeypatch):
    w = world
    hold(w, rooms=1)

    def down(link_id):
        raise PaymentError("razorpay down")

    monkeypatch.setattr(w.pay, "link_status", down, raising=False)
    assert w.bot.check_payments() == 0
    assert w.bot.sweep() is not None  # the sweeper keeps running


def test_payment_webhook_twice_confirms_once(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    assert w.bot.mark_paid(link_id=b.payment_link_id, payment_id="pay_1") == "paid"
    before = len(w.wa.texts())
    assert w.bot.mark_paid(link_id=b.payment_link_id, payment_id="pay_1") == "already_paid"
    assert len(w.wa.texts()) == before


# --------------------------------------------------------------------------- 4. voice


@pytest.mark.parametrize("problem", ["exception", "none", "empty"])
def test_voice_failures_get_one_polite_text(world, problem):
    w = world

    class Voice:
        def process(self, media_id):
            if problem == "exception":
                raise RuntimeError("ffmpeg crashed")
            if problem == "empty":
                return type("R", (), {"text": "   ", "problem": None})()
            return type("R", (), {"text": None, "problem": "failed"})()

    w.bot._voice_in = lambda: Voice()
    out = w.bot.handle(msg(kind="audio", media_id="m1"))
    assert out == "voice_problem"
    assert len(w.wa.texts()) == 1 and all(k == "text" for _, k, _ in w.wa.to(GUEST))


# --------------------------------------------------------------------------- 5. odd input and abuse


@pytest.mark.parametrize("kind", ["reaction", "sticker", "unsupported", "system", "request_welcome"])
def test_silent_message_types_get_no_reply(world, kind):
    w = world
    assert w.bot.handle(msg(kind=kind)) == "unsupported"
    assert w.wa.texts() == []


@pytest.mark.parametrize("kind", ["location", "contacts", "video"])
def test_other_media_get_one_short_text(world, kind):
    w = world
    w.bot.handle(msg(kind=kind))
    assert len(w.wa.texts()) == 1


def test_empty_or_whitespace_text_is_ignored(world):
    w = world
    assert w.bot.handle(msg("   ")) in ("unsupported", "quiet")
    assert w.wa.texts() == []


def test_a_4000_character_message_is_handled(world):
    w = world
    assert say(w, "is it near the beach? " + "x" * 4000, GUEST, reply("Yes, right on the beach.")) == "answered"
    assert all(len(m.content) < 3000 for m in w.llm.calls[-1].messages)  # long text is cut before the AI


def test_another_guests_number_never_shows_their_booking(world):
    w = world
    hold(w, rooms=1, phone="+919811111111")
    say(w, "show booking of 9811111111", GUEST, tool("my_bookings"), reply("[no reply]"))
    from test_bot import last_tool
    assert last_tool(w)["bookings"] == []  # my_bookings is always the sender's own number


def test_sql_like_text_is_just_text(world):
    w = world
    say(w, "'; DROP TABLE bookings; --", GUEST, reply("How can I help?"))
    assert w.repo.list_bookings() == [] and w.repo.list_chat(GUEST)


def test_one_guest_flooding_messages_is_capped(world):
    w = world
    calls = 0
    for i in range(70):
        w.llm.script.append(reply(f"Answer {i}."))
        w.bot.handle(msg(f"question {i} about the pool?"))
    calls = len(w.llm.calls)
    assert calls <= flow.MAX_AI_TURNS_PER_HOUR  # one guest cannot burn unlimited AI calls
    assert w.repo.is_paused(GUEST)[0]  # handed to the team once the cap is hit
    assert sum("too many messages" in x for x in w.wa.texts(STAFF)) == 1
    w.llm.script.clear()


# --------------------------------------------------------------------------- 6. restart safety


def test_a_restart_mid_booking_keeps_the_booking_state(world):
    w = world
    say(w, "Alleppey, 10 nov to 12 nov, 2 people", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", check_out="2026-11-12", adults=2),
        reply("[no reply]"))
    fresh = flow.BookingBot(w.bot.config, w.repo, w.wa, w.pay, w.clock, llm=lambda: w.llm)  # a new process
    w.bot = fresh
    say(w, "Asha, 9847012345", GUEST, tool("create_booking", guest_name="Asha", mobile="9847012345"),
        reply("[no reply]"))
    assert w.repo.list_bookings(phone=GUEST)[0].status == "hold" and w.wa.texts()[-1].startswith("Please pay")


def test_reminders_are_not_sent_twice_after_a_restart(world):
    w = world
    now = w.clock.now()
    tomorrow = (now + timedelta(days=1)).date()
    b = Booking(w.p1.id, w.deluxe.id, GUEST, tomorrow, tomorrow + timedelta(days=1), 2, 0, 1, 4000, 2000, now,
                status="paid", guest_name="Asha", amount_paid=2000, paid_at=now)
    w.repo.create_hold(b, now)
    w.clock.now = lambda: now.replace(hour=10)
    first = w.bot.remind_ids(w.clock.now())
    fresh = flow.BookingBot(w.bot.config, w.repo, w.wa, w.pay, w.clock, llm=lambda: w.llm)
    assert first == 1 and fresh.remind_ids(w.clock.now()) == 0
