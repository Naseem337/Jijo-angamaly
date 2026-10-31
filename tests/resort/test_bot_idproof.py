"""ID proof after payment, the day-before reminder, feedback after checkout, and the ID photo purge."""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta

import pytest

from app.core.clock import IST, FixedClock
from app.providers.llm import MockLLM
from app.resort import feedback
from app.resort.config import load_resort_config
from app.resort.db import ResortRepo
from app.resort.flow import BookingBot
from app.resort.models import Booking, Property, RoomType
from app.resort.payments import MockPayments
from app.whatsapp.client import WhatsAppError
from app.whatsapp.parse import InboundMessage

STAFF = "+919800000001"
GUEST = "+919847012345"
_ids = itertools.count(1)


class FakeWA:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, object]] = []
        self.media: dict[str, object] = {}  # media id -> (bytes, mime) or an exception
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

    def download_media(self, media_id, *, max_bytes=0):
        value = self.media[media_id]
        if isinstance(value, Exception):
            raise value
        return value

    def of(self, kind, phone=GUEST):
        return [p for to, k, p in self.sent if to == phone and k == kind]

    def all_text(self, phone=GUEST) -> str:
        parts = []
        for to, kind, payload in self.sent:
            if to == phone:
                parts.append(payload if kind == "text" else str(payload))
        return "\n".join(parts)


@pytest.fixture()
def world(tmp_path):
    repo = ResortRepo(tmp_path / "r.sqlite3")
    prop = repo.save_property(Property("Pozhiyoram Resort", city="Kerala", advance_pct=50, staff_numbers=[STAFF]))
    rt = repo.save_room_type(RoomType(prop.id, "Deluxe", units=5, max_adults=2, max_children=1, max_occupancy=3,
                                      base_rate=4000))
    clock = FixedClock(datetime(2026, 11, 2, 10, 0, tzinfo=IST))
    wa = FakeWA()
    bot = BookingBot(load_resort_config(), repo, wa, MockPayments(), clock, llm=lambda: MockLLM())
    repo.set_state(GUEST, {"lang": "en", "step": "menu"})
    return type("W", (), dict(repo=repo, wa=wa, clock=clock, bot=bot, prop=prop, rt=rt))()


def make_booking(w, *, adults=1, check_in=date(2026, 11, 10), nights=2, phone=GUEST) -> Booking:
    b = Booking(w.prop.id, w.rt.id, phone, check_in, check_in + timedelta(days=nights), adults, 0, 1, 8000, 4000,
                w.clock.now(), guest_name="Asha", hold_expires_at=w.clock.now() + timedelta(minutes=15))
    return w.repo.create_hold(b, w.clock.now())


def pay(w, b: Booking) -> None:
    assert w.bot.mark_paid(booking_id=b.id) == "paid"


def say(w, text, phone=GUEST):
    return w.bot.handle(InboundMessage(f"wamid.{next(_ids)}", phone, "text", datetime(2026, 11, 2, 4, 30), text=text))


def send_image(w, media_id="m1", data=b"\xff\xd8\xffJPEG", mime="image/jpeg", phone=GUEST, kind="image"):
    w.wa.media[media_id] = (data, mime)
    return w.bot.handle(InboundMessage(f"wamid.{next(_ids)}", phone, kind, datetime(2026, 11, 2, 4, 30),
                                       media_id=media_id))


def id_asks(w):
    """The plain-text ID requests sent so far."""
    return [x for x in w.wa.of("text") if "ID" in x and "photo" in x and "starts tomorrow" not in x]


# --------------------------------------------------------------------------- asking once


def test_paid_booking_asks_for_id_once_in_plain_text(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    asks = id_asks(w)
    assert len(asks) == 1 and "guest 1 of 1" in asks[0] and "30 days" in asks[0]
    assert "Aadhaar" in asks[0] and "show it at check-in" in asks[0]
    assert w.wa.of("list") == [] and w.wa.of("buttons") == []  # no tap list anywhere
    assert w.bot.mark_paid(booking_id=b.id) == "already_paid"
    w.bot.announce_paid(w.repo.get_booking(b.id))  # staff path announcing again
    assert len(id_asks(w)) == 1


def test_not_asked_outside_the_24h_window(world):
    w = world
    w.wa.text_error = WhatsAppError("outside window", status=400, code=131047)
    pay(w, make_booking(w))
    assert id_asks(w) == [] and "idp" not in w.repo.get_state(GUEST)


# --------------------------------------------------------------------------- photo


def test_photo_is_stored_with_retention(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    assert send_image(w) == "id_done"
    proofs = w.repo.list_id_proofs(b.id, with_data=True)
    assert len(proofs) == 1
    p = proofs[0]
    assert (p.guest_no, p.status, p.mime, p.data) == (1, "received", "image/jpeg", b"\xff\xd8\xffJPEG")
    assert p.delete_after == b.check_out + timedelta(days=30)
    assert "All set" in w.wa.of("text")[-1]
    assert "idp" not in w.repo.get_state(GUEST)


def test_non_image_is_rejected_and_nothing_stored(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    assert send_image(w, "doc1", b"%PDF", "application/pdf", kind="document") == "id_rejected"
    assert "photo" in w.wa.of("text")[-1].lower()
    assert w.repo.list_id_proofs(b.id) == []
    assert "idp" in w.repo.get_state(GUEST)  # still waiting for a real photo


def test_download_errors_ask_again(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    w.wa.media["big"] = WhatsAppError("media too large (9000000 bytes)")
    assert w.bot.handle(InboundMessage("wamid.big", GUEST, "image", datetime(2026, 11, 2, 4, 30),
                                       media_id="big")) == "id_failed"
    assert "5 MB" in w.wa.of("text")[-1]
    assert w.repo.list_id_proofs(b.id) == []


# --------------------------------------------------------------------------- later, several guests


def test_saying_they_will_show_it_at_checkin_stores_a_pending_proof(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    assert say(w, "I will show it at check-in") == "id_done"
    p = w.repo.list_id_proofs(b.id)[0]
    assert (p.id_type, p.status, p.data) == ("later", "pending", b"")
    assert "show your ID at check-in" in w.wa.of("text")[-1]


def test_a_question_while_waiting_is_not_taken_as_skipping(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    llm = MockLLM([MockLLM.text("Check-in is from 2 PM.")])
    w.bot._llm = lambda: llm
    assert say(w, "what time is check in?") == "answered"  # goes to the AI, the ID request stays open
    assert w.repo.list_id_proofs(b.id) == [] and "idp" in w.repo.get_state(GUEST)


def test_two_adults_ask_for_guest_two_then_finish(world):
    w = world
    b = make_booking(w, adults=2)
    pay(w, b)
    assert send_image(w, "g1") == "id_next_guest"
    assert "guest 2 of 2" in w.wa.of("text")[-1] and "30 days" not in w.wa.of("text")[-1]
    assert send_image(w, "g2", b"\x89PNG", "image/png") == "id_done"
    assert [(p.guest_no, p.mime) for p in w.repo.list_id_proofs(b.id)] == [(1, "image/jpeg"), (2, "image/png")]


def test_later_for_the_rest_marks_remaining_guests_pending(world):
    w = world
    b = make_booking(w, adults=3)
    pay(w, b)
    send_image(w)
    assert say(w, "the others will show it later") == "id_done"
    assert [(p.guest_no, p.status) for p in w.repo.list_id_proofs(b.id)] == [
        (1, "received"), (2, "pending"), (3, "pending")]


def test_second_paid_booking_waits_its_turn(world):
    w = world
    first = make_booking(w, check_in=date(2026, 11, 10))
    second = make_booking(w, check_in=date(2026, 12, 10))
    pay(w, first)
    pay(w, second)
    assert len(id_asks(w)) == 1  # only the first is asked now
    assert w.repo.get_state(GUEST)["idp_queue"] == [second.id]
    say(w, "later")  # finishes the first booking, which brings up the second
    assert len(id_asks(w)) == 2
    assert w.repo.get_state(GUEST)["idp"]["booking_id"] == second.id
    say(w, "later")
    assert "idp" not in w.repo.get_state(GUEST)


def test_image_without_an_open_request_is_still_unsupported(world):
    w = world
    assert send_image(w) == "unsupported"


# --------------------------------------------------------------------------- reminder and purge


def test_reminder_goes_once_the_day_before_check_in(world):
    w = world
    b = make_booking(w, check_in=date(2026, 11, 3))  # tomorrow
    w.wa.text_error = WhatsAppError("outside window", status=400, code=131047)
    pay(w, b)  # outside the window: no ID request at payment time
    assert id_asks(w) == []
    w.wa.text_error = None
    w.clock.set(datetime(2026, 11, 2, 8, 0, tzinfo=IST))
    assert w.bot.sweep().get("id_reminders") is None  # before 09:00 IST: wait
    w.clock.set(datetime(2026, 11, 2, 11, 0, tzinfo=IST))
    assert w.bot.sweep()["id_reminders"] == 1
    assert "starts tomorrow" in w.wa.of("text")[-1]
    assert w.repo.get_state(GUEST)["idp"]["booking_id"] == b.id  # their next photo counts as the ID
    assert send_image(w) == "id_done"
    assert "id_reminders" not in w.bot.sweep()  # only once


def test_reminder_uses_the_template_outside_the_window_and_skips_guests_with_proofs(world, monkeypatch):
    w = world
    monkeypatch.setenv("RESORT_TPL_ID_REMINDER", "resort_id_reminder")
    pay(w, make_booking(w, check_in=date(2026, 11, 3), phone=GUEST))
    done = make_booking(w, check_in=date(2026, 11, 3), phone="+919847000002")
    pay(w, done)
    say(w, "later", "+919847000002")  # promised to show it at check-in: no reminder needed
    w.repo.set_state(GUEST, {"lang": "en"})
    w.wa.text_error = WhatsAppError("outside window", status=400, code=131047)
    w.clock.set(datetime(2026, 11, 2, 11, 0, tzinfo=IST))
    assert w.bot.sweep()["id_reminders"] == 1
    sent = w.wa.of("template")
    assert sent and sent[0]["name"] == "resort_id_reminder"


def test_expired_id_photos_are_purged_once_a_day(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    send_image(w)
    proof = w.repo.list_id_proofs(b.id)[0]
    proof.delete_after = date(2026, 11, 1)  # yesterday
    w.repo.save_id_proof(proof)
    assert w.bot.sweep()["id_purged"] == 1
    assert w.repo.list_id_proofs(b.id) == []
    assert "id_purged" not in w.bot.sweep()  # already ran today


# --------------------------------------------------------------------------- feedback


def test_feedback_is_one_plain_reply(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    assert w.bot.announce_checked_out(w.repo.get_booking(b.id)) is True
    texts = w.wa.of("text")
    assert "Thank you for staying" in texts[-2] and "rating from 1" in texts[-1]
    assert w.wa.of("list") == [] and w.wa.of("buttons") == []
    assert say(w, "4 lovely pool and great food") == "feedback_done"
    row = feedback.list_feedback(w.repo, w.prop.id)[0]
    assert (row["booking_id"], row["rating"], row["comment"]) == (b.id, 4, "lovely pool and great food")
    assert feedback.average_rating(w.repo) == 4.0
    assert "fb" not in w.repo.get_state(GUEST)


def test_feedback_parsing_never_takes_a_number_from_the_middle_of_a_sentence():
    parse = BookingBot._parse_feedback
    assert parse("5") == (5, "") and parse("4/5 great food") == (4, "great food")
    assert parse("★★★★ nice") == (4, "nice") and parse("great stay 5") == (5, "great stay")
    assert parse("we booked 2 rooms and loved it") is None
    assert parse("what time is checkout") is None


def test_feedback_can_be_skipped(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    send_image(w)  # the ID request is finished first
    w.bot.announce_checked_out(w.repo.get_booking(b.id))
    assert say(w, "skip") == "feedback_skipped"
    assert feedback.list_feedback(w.repo) == [] and "fb" not in w.repo.get_state(GUEST)


def test_feedback_not_asked_when_the_guest_cannot_be_reached(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    w.wa.text_error = WhatsAppError("outside window", status=400, code=131047)
    assert w.bot.announce_checked_out(w.repo.get_booking(b.id)) is False
    assert "fb" not in w.repo.get_state(GUEST)
