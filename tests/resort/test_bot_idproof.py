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


def tap(w, rid, phone=GUEST):
    return w.bot.handle(InboundMessage(f"wamid.{next(_ids)}", phone, "interactive", datetime(2026, 11, 2, 4, 30),
                                       text=rid, reply_id=rid))


def say(w, text, phone=GUEST):
    return w.bot.handle(InboundMessage(f"wamid.{next(_ids)}", phone, "text", datetime(2026, 11, 2, 4, 30), text=text))


def send_image(w, media_id="m1", data=b"\xff\xd8\xffJPEG", mime="image/jpeg", phone=GUEST, kind="image"):
    w.wa.media[media_id] = (data, mime)
    return w.bot.handle(InboundMessage(f"wamid.{next(_ids)}", phone, kind, datetime(2026, 11, 2, 4, 30),
                                       media_id=media_id))


def id_lists(w):
    return [p for p in w.wa.of("list") if "id:aadhaar" in str(p["rows"])]


# --------------------------------------------------------------------------- asking once


def test_paid_booking_asks_for_id_once(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    lists = id_lists(w)
    assert len(lists) == 1
    assert "Guest 1 of 1" in lists[0]["body"] and "30 days" in lists[0]["body"]
    assert [r[0] for r in lists[0]["rows"]] == ["id:aadhaar", "id:passport", "id:dl", "id:voter", "id:other",
                                                "id:later"]
    assert all(len(r[1]) <= 24 for r in lists[0]["rows"])
    assert w.bot.mark_paid(booking_id=b.id) == "already_paid"
    w.bot.announce_paid(w.repo.get_booking(b.id))  # staff path announcing again
    assert len(id_lists(w)) == 1


def test_not_asked_outside_the_24h_window(world):
    w = world
    w.wa.text_error = WhatsAppError("outside window", status=400, code=131047)
    pay(w, make_booking(w))
    assert id_lists(w) == []


# --------------------------------------------------------------------------- photo, last 4 digits


def test_aadhaar_then_photo_is_stored_with_retention(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    assert tap(w, "id:aadhaar") == "id_photo_requested"
    assert "Aadhaar" in w.wa.of("text")[-1]
    assert send_image(w) == "id_received"
    proofs = w.repo.list_id_proofs(b.id, with_data=True)
    assert len(proofs) == 1
    p = proofs[0]
    assert (p.guest_no, p.id_type, p.status, p.mime, p.data) == (1, "aadhaar", "received", "image/jpeg",
                                                                 b"\xff\xd8\xffJPEG")
    assert p.delete_after == b.check_out + timedelta(days=30)
    assert w.wa.of("buttons")[-1]["buttons"][0][0] == "id:skip"
    assert tap(w, "id:skip") == "id_done"
    assert "All set" in w.wa.of("text")[-1]
    assert "idp" not in w.repo.get_state(GUEST)


def test_non_image_is_rejected_and_nothing_stored(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    tap(w, "id:passport")
    assert send_image(w, "doc1", b"%PDF", "application/pdf", kind="document") == "id_rejected"
    assert "photo" in w.wa.of("text")[-1].lower()
    assert w.repo.list_id_proofs(b.id) == []
    assert w.repo.get_state(GUEST)["idp"]["stage"] == "photo"  # still waiting for a real photo


def test_download_errors_ask_again(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    tap(w, "id:voter")
    w.wa.media["big"] = WhatsAppError("media too large (9000000 bytes)")
    assert w.bot.handle(InboundMessage("wamid.big", GUEST, "image", datetime(2026, 11, 2, 4, 30),
                                       media_id="big")) == "id_failed"
    assert "5 MB" in w.wa.of("text")[-1]
    assert w.repo.list_id_proofs(b.id) == []


def test_typed_long_number_keeps_only_last_four_and_is_never_echoed(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    tap(w, "id:aadhaar")
    send_image(w)
    assert say(w, "1234 5678 9012") == "id_done"
    proof = w.repo.list_id_proofs(b.id)[0]
    assert proof.last4 == "9012"
    out = w.wa.all_text()
    assert "123456789012" not in out and "1234 5678" not in out and "9012" not in out


def test_too_few_digits_asks_again(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    tap(w, "id:aadhaar")
    send_image(w)
    assert say(w, "12") == "id_last4_retry"
    assert w.repo.list_id_proofs(b.id)[0].last4 == ""


# --------------------------------------------------------------------------- later, several guests


def test_later_option_stores_a_pending_proof(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    assert tap(w, "id:later") == "id_done"
    p = w.repo.list_id_proofs(b.id)[0]
    assert (p.id_type, p.status, p.data) == ("later", "pending", b"")
    assert "show your ID at check-in" in w.wa.of("text")[-1]


def test_two_adults_ask_for_guest_two_then_finish(world):
    w = world
    b = make_booking(w, adults=2)
    pay(w, b)
    tap(w, "id:aadhaar")
    send_image(w, "g1")
    assert tap(w, "id:skip") == "id_next_guest"
    nxt = id_lists(w)[-1]
    assert "Guest 2 of 2" in nxt["body"] and "30 days" not in nxt["body"]
    assert "id:skiprest" not in str(nxt["rows"])  # the last guest has nothing left to skip
    tap(w, "id:passport")
    send_image(w, "g2", b"\x89PNG", "image/png")
    assert tap(w, "id:skip") == "id_done"
    assert [(p.guest_no, p.id_type, p.mime) for p in w.repo.list_id_proofs(b.id)] == [
        (1, "aadhaar", "image/jpeg"), (2, "passport", "image/png")]


def test_skip_the_rest_marks_remaining_guests_pending(world):
    w = world
    b = make_booking(w, adults=3)
    pay(w, b)
    assert "id:skiprest" in str(id_lists(w)[0]["rows"])
    tap(w, "id:aadhaar")
    send_image(w)
    tap(w, "id:skip")
    assert tap(w, "id:skiprest") == "id_done"
    assert [(p.guest_no, p.status) for p in w.repo.list_id_proofs(b.id)] == [
        (1, "received"), (2, "pending"), (3, "pending")]


def test_second_paid_booking_waits_its_turn(world):
    w = world
    first = make_booking(w, check_in=date(2026, 11, 10))
    second = make_booking(w, check_in=date(2026, 12, 10))
    pay(w, first)
    pay(w, second)
    assert len(id_lists(w)) == 1  # only the first is asked now
    assert w.repo.get_state(GUEST)["idp_queue"] == [second.id]
    tap(w, "id:later")  # finishes the first booking, which brings up the second
    assert len(id_lists(w)) == 2
    assert w.repo.get_state(GUEST)["idp"]["booking_id"] == second.id
    tap(w, "id:later")
    assert "idp" not in w.repo.get_state(GUEST)


def test_image_without_an_open_request_is_still_unsupported(world):
    w = world
    assert send_image(w) == "unsupported"


# --------------------------------------------------------------------------- reminder and purge


def test_reminder_goes_once_the_day_before_check_in(world):
    w = world
    b = make_booking(w, check_in=date(2026, 11, 3))  # tomorrow
    w.wa.text_error = WhatsAppError("outside window", status=400, code=131047)
    pay(w, b)  # outside the window: no ID list at payment time
    assert id_lists(w) == []
    w.wa.text_error = None
    w.clock.set(datetime(2026, 11, 2, 8, 0, tzinfo=IST))
    assert w.bot.sweep().get("id_reminders") is None  # before 09:00 IST: wait
    w.clock.set(datetime(2026, 11, 2, 11, 0, tzinfo=IST))
    assert w.bot.sweep()["id_reminders"] == 1
    assert "starts tomorrow" in w.wa.of("text")[-1]
    assert len(id_lists(w)) == 1  # inside the window the option list comes with it
    assert "id_reminders" not in w.bot.sweep()  # only once


def test_reminder_uses_the_template_outside_the_window_and_skips_guests_with_proofs(world, monkeypatch):
    w = world
    monkeypatch.setenv("RESORT_TPL_ID_REMINDER", "resort_id_reminder")
    pay(w, make_booking(w, check_in=date(2026, 11, 3), phone=GUEST))
    w.repo.set_state("+919847000002", {"lang": "en", "step": "menu"})
    done = make_booking(w, check_in=date(2026, 11, 3), phone="+919847000002")
    pay(w, done)
    tap(w, "id:later", "+919847000002")  # promised to show it at check-in: no reminder needed
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
    tap(w, "id:aadhaar")
    send_image(w)
    proof = w.repo.list_id_proofs(b.id)[0]
    proof.delete_after = date(2026, 11, 1)  # yesterday
    w.repo.save_id_proof(proof)
    assert w.bot.sweep()["id_purged"] == 1
    assert w.repo.list_id_proofs(b.id) == []
    assert "id_purged" not in w.bot.sweep()  # already ran today


# --------------------------------------------------------------------------- feedback


def test_feedback_rating_and_comment_are_stored(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    assert w.bot.announce_checked_out(w.repo.get_booking(b.id)) is True
    rating_list = w.wa.of("list")[-1]
    assert [r[0] for r in rating_list["rows"]] == ["fb:5", "fb:4", "fb:3", "fb:2", "fb:1"]
    assert "Thank you for staying" in w.wa.of("text")[-1]
    assert tap(w, "fb:4") == "feedback_rating"
    assert feedback.list_feedback(w.repo)[0]["rating"] == 4
    assert say(w, "Lovely pool and great food") == "feedback_done"
    row = feedback.list_feedback(w.repo, w.prop.id)[0]
    assert (row["booking_id"], row["rating"], row["comment"]) == (b.id, 4, "Lovely pool and great food")
    assert feedback.average_rating(w.repo) == 4.0
    assert "fb" not in w.repo.get_state(GUEST)


def test_feedback_comment_can_be_skipped(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    w.bot.announce_checked_out(w.repo.get_booking(b.id))
    tap(w, "fb:5")
    assert tap(w, "fb:skip") == "feedback_done"
    assert feedback.list_feedback(w.repo)[0]["comment"] == ""


def test_feedback_not_asked_when_the_guest_cannot_be_reached(world):
    w = world
    b = make_booking(w)
    pay(w, b)
    w.wa.text_error = WhatsAppError("outside window", status=400, code=131047)
    assert w.bot.announce_checked_out(w.repo.get_booking(b.id)) is False
    assert "fb" not in w.repo.get_state(GUEST)
