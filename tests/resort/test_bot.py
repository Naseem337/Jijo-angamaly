from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import re
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
from app.whatsapp.parse import InboundMessage, parse_echoes, parse_webhook

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

    def upload_media(self, data, mime_type="image/jpeg", filename="x"):
        return f"media-{len(data)}-{filename}"

    def send_image(self, to, media_id, caption=""):
        self.sent.append((to, "image", media_id))
        return "wamid.x"

    def send_document(self, to, media_id, filename, caption=""):
        self.sent.append((to, "document", filename))
        return "wamid.x"

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
    return say(w, "Asha Menon, 9847012345", phone, tool("create_booking", guest_name="Asha Menon", mobile="9847012345"),
               reply("[no reply]"))


def hold(w, rooms=1, nights=2, rt="Deluxe", phone=GUEST):
    """A guest chats all the way to a held booking with a payment link."""
    to_summary(w, rt=rt, rooms=rooms, nights=nights, adults=2 * rooms, phone=phone)
    return confirm(w, phone)


# --------------------------------------------------------------------------- the conversation


def test_hi_gets_the_owners_greeting_with_no_menu_and_no_ai_call(world):
    w = world
    assert say(w, "hii") == "greeting"
    assert w.wa.last() == (GUEST, "text", "Hi, How may I assist you?")
    assert [k for _, k, _ in w.wa.to(GUEST)] == ["text"] and w.llm.calls == []


def test_greeting_in_other_scripts_gets_the_same_line_in_english_letters(world):
    w = world
    say(w, "ഹായ്")
    assert w.wa.texts()[-1] == "Hi, How may I assist you?" and w.repo.get_state(GUEST)["lang"] == "ml"
    say(w, "नमस्ते", "+919847000099")
    assert w.wa.texts("+919847000099")[-1] == "Hi, How may I assist you?"


def test_fixed_messages_are_never_in_native_script():
    for key in S:
        for lang in ("ml", "ta", "hi", "mn", "en"):
            assert not re.search(r"[ऀ-෿]", t(key, lang)), (key, lang)


def test_a_booking_request_is_asked_which_resort_including_one_not_open_online(world):
    w = world
    w.repo.save_property(Property(**{**w.p2.__dict__, "active": False}))  # Kanthalloor: no online booking yet
    say(w, "resort booking", GUEST)
    sent = w.wa.texts()
    towns = [(p.city or p.name).split(",")[0].strip() for p in w.repo.list_properties(active_only=False)]
    assert sent == [f"Sure, Kindly confirm the Location 📍\n{' or '.join(towns)} ?"]  # the AI's own line is not sent
    say(w, "is it near the beach?", GUEST, reply("Yes."))
    assert len(towns) == 2 and "currently not available" in w.llm.calls[-1].system


def test_the_ai_asking_which_location_in_its_own_words_is_replaced_by_the_owners_question(world):
    w = world
    towns = " or ".join(w.bot._towns())
    say(w, "resort booking", GUEST, reply(f"Sure, which location sir - {towns.replace(' or ', ' or ')}?"))
    assert w.wa.texts() == [f"Sure, Kindly confirm the Location 📍\n{towns} ?"]
    assert say(w, "allepy") == "form"  # and the answer to it is understood as usual


def test_hi_in_the_middle_of_a_chat_carries_on_instead_of_starting_again(world):
    w = world
    assert say(w, "hi") == "greeting"
    say(w, "need a room in Pozhiyoram", GUEST, reply("Sure. Which dates?"))
    form = "Please share these details:\n\nCheck in :\nCheck out :\nNumber of persons :"
    assert w.wa.texts()[-1] == form  # never the AI's own 'Which dates?' (client, 2026-11-18)
    assert say(w, "hi") == "greeting"  # mid-chat: the owner's greeting, no AI call (2026-11-18)
    assert w.wa.texts()[-2:] == [form, "Hi, How may I assist you?"]
    say(w, "2 people", GUEST, reply("[no reply]"))
    # the chat is remembered: the resort is still known, so only the missing dates are asked (in code, 2026-11-19)
    assert w.wa.texts()[-1] == "Please share these details:\n\nCheck in :\nCheck out :"


def test_english_with_place_and_room_words_stays_english():
    from app.resort.chat import latin_style
    for text in ("Pozhiyoram rooms available?", "Alleppey, 10 Nov 2 nights, 2 persons", "resort booking",
                 "what is the price for deluxe room"):
        assert latin_style(text) == "en", text
    for text in ("room undo", "nale check in cheyyan pattumo", "ethra aakum"):
        assert latin_style(text) == "manglish", text


def test_a_swipe_reply_tells_the_ai_which_message_the_guest_answers(world):
    w = world
    w.wa.send_text = lambda to, text: (w.wa.sent.append((to, "text", text)), f"wamid.out{len(w.wa.sent)}")[1]
    say(w, "is Pozhiyoram near the beach", GUEST, reply("Yes, right on the beach."))
    quoted = f"wamid.out{len(w.wa.sent)}"
    say(w, "pool undo", GUEST, reply("No pool, but the sea is right there."))
    w.llm.script.append(reply("Yes, a few steps."))
    m = InboundMessage("wamid.swipe1", GUEST, "text", datetime(2026, 11, 2, 4, 31), text="how far", context_id=quoted)
    w.bot.handle(m)
    last = w.llm.calls[-1].messages[-1].content
    assert 'swiped to reply to your (the assistant\'s) message: "Yes, right on the beach."' in last
    say(w, "ok thanks bye", GUEST, reply("[no reply]"))
    assert "swiped" not in w.llm.calls[-1].messages[-1].content  # only for that one turn


def test_manglish_guest_gets_one_rewrite_when_the_ai_answers_in_english(world):
    w = world
    say(w, "room undo nale", GUEST, reply("Yes, we have rooms available for tomorrow."),
        reply("Und sir, nale rooms und."))
    assert w.wa.texts()[-1] == "Und sir, nale rooms und."


def test_photos_uploaded_in_the_dashboard_are_sent_when_a_guest_asks_and_never_twice(world, tmp_path, monkeypatch):
    from app.resort import photos as ph
    monkeypatch.setenv("RESORT_DB_PATH", str(tmp_path / "r.sqlite3"))
    monkeypatch.setattr(ph, "_shrink", lambda data: None)
    w = world
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 50
    room_refs = [ph.save_photo(png) for _ in range(2)]
    prop_refs = [ph.save_photo(png) for _ in range(4)]
    w.repo.save_room_type(RoomType(**{**w.deluxe.__dict__, "photo_urls": room_refs}))
    w.repo.save_property(Property(**{**w.repo.get_property(w.p1.id).__dict__, "photo_urls": prop_refs}))
    assert say(w, "deluxe room photos undo?") == "next_step"  # before the dates: the next step, not photos
    assert w.wa.texts()[-1].startswith("Sure, Kindly confirm the Location") and not w.wa.to(GUEST)[:-1]
    w.wa.sent.clear()
    st = w.repo.get_state(GUEST)
    st["checked"] = "rooms were found"  # after the availability check, photos go on request
    w.repo.set_state(GUEST, st)
    say(w, "deluxe room photos undo?", GUEST, tool("send_photos", place="Pozhiyoram", room_type="Deluxe"), reply("[no reply]"))
    images = [p for _, k, p in w.wa.to(GUEST) if k == "image"]
    assert images and len(images) == 2 and w.wa.texts() == []  # the room's own photos only, never the general ones
    say(w, "more?", GUEST, tool("send_photos", place="Pozhiyoram"), reply("That's all the photos we have sir."))
    assert w.wa.texts()[-1] == "All the photos are sent above."  # fixed line, never the AI's own
    assert len([1 for _, k, _ in w.wa.to(GUEST) if k == "image"]) == 2


def test_after_the_form_rooms_ok_then_glimpse_then_general_then_room_photos_then_name(world, tmp_path, monkeypatch):
    from app.resort import photos as ph
    monkeypatch.setenv("RESORT_DB_PATH", str(tmp_path / "r.sqlite3"))
    monkeypatch.setattr(ph, "_shrink", lambda data: None)
    w = world
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 50
    general = [ph.save_photo(png) for _ in range(8)]
    room = [ph.save_photo(png) for _ in range(2)]
    w.repo.save_property(Property(**{**w.repo.get_property(w.p1.id).__dict__, "photo_urls": general,
                                     "intro": "Your Beachfront Hideout Awaits!\n{room} - {price}/Night/Room"}))
    w.repo.save_room_type(RoomType(**{**w.deluxe.__dict__, "photo_urls": room}))
    say(w, "I need a resort", GUEST)
    assert say(w, "Alleppey") == "form"
    assert [k for _, k, _ in w.wa.to(GUEST)] == ["text", "text"]  # no photos before the form any more
    start = len(w.wa.to(GUEST))
    say(w, "Check in :dec 12\nCheck out :dec 15\nNumber of persons :2", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-12-12", check_out="2026-12-15", adults=2),
        reply("[no reply]"))
    sent = [(k, p) for _, k, p in w.wa.to(GUEST)][start:]
    assert [k for k, _ in sent] == ["text", "text"] + ["image"] * 10 + ["text"]  # ALL 8 general + the room's 2
    assert sent[0][1] == "Yes, rooms are available for your dates."
    assert sent[1][1] == "Your Beachfront Hideout Awaits!\nDeluxe - ₹4,000/Night/Room"
    assert sent[-1][1] == "Please share your name and mobile number to proceed."
    logged = [m.text[len("[photo] "):] for m in w.repo.list_chat(GUEST, limit=100) if m.text.startswith("[photo] ")]
    assert logged == general + room  # all general photos in the owner's order, then ONLY the offered room's own


@pytest.mark.parametrize("answer, place", [("allepy", "p1"), ("Aleppey pls", "p1"), ("alappuzha", "p1"),
                                           ("kanthaloor", "p2"), ("poziyoram", "p1")])
def test_a_misspelt_place_is_understood_and_the_form_follows(world, answer, place):
    w = world
    say(w, "I need a resort", GUEST)
    assert say(w, answer) == "form"
    assert w.repo.get_state(GUEST)["form_place"] == getattr(w, place).id
    assert w.wa.texts()[-1].startswith("Please share these details")


FORM = "Please share these details:\n\nCheck in :\nCheck out :\nNumber of persons :"


def test_restart_starts_a_fresh_conversation_and_keeps_bookings(world):
    w = world
    say(w, "Alleppey, 12 dec to 15 dec, 3 persons", GUEST, reply("[no reply]"))
    assert say(w, "Restart") == "restart"
    assert w.wa.texts()[-1] == "Hi, How may I assist you?" and "bk" not in w.repo.get_state(GUEST)
    assert say(w, "hi") == "greeting"  # a fresh start greets again
    say(w, "is it near the beach?", GUEST, reply("Yes, right on the beach."))
    seen = "\n".join(m.content for m in w.llm.calls[-1].messages)
    assert "12 dec" not in seen and "[restart]" not in seen  # the old conversation is gone from the AI's view


def test_photos_do_not_push_the_guests_details_out_of_the_ais_memory(world):
    w = world
    say(w, "Alleppey, 12 dec to 15 dec, 3 persons", GUEST, reply("[no reply]"))
    for i in range(30):  # a glimpse with many photos: each is stored as its own row
        w.bot._log(GUEST, f"[photo] /resort/media/x{i}.jpg")
    say(w, "Rahul", GUEST, reply("[no reply]"))
    seen = "\n".join(m.content for m in w.llm.calls[-1].messages)
    assert "12 dec to 15 dec, 3 persons" in seen and "[photos sent: 30]" in seen and "x7.jpg" not in seen


def test_the_conversation_is_remembered_through_a_long_pause_the_same_day(world):
    w = world
    say(w, "Alleppey, 12 dec to 15 dec, 3 persons", GUEST, reply("[no reply]"))
    w.clock.advance(hours=14)  # the guest comes back the next day
    say(w, "hi, any update?", GUEST, reply("[no reply]"))
    assert any("12 dec to 15 dec" in m.content for m in w.llm.calls[-1].messages)


def test_the_ai_never_asks_dates_in_its_own_words_the_form_goes(world):
    w = world
    say(w, "is Alleppey nice in december", GUEST, reply("Rooms are available in Alleppey. What is your check-in date?"))
    assert w.wa.texts() == [FORM]
    say(w, "2 people", GUEST, reply("Yes, right on the beach. Please share your check-in and check-out dates."))
    # the answer stays, the ask becomes the form with only the missing lines (they gave '2 people')
    # '2 people' is stay details: the code asks only the missing lines, the AI is not needed (2026-11-19)
    assert w.wa.texts()[-2:] == [FORM, "Please share these details:\n\nCheck in :\nCheck out :"]
    w.llm.script.clear()


def test_a_room_request_the_ai_leaves_silent_still_gets_the_location_question(world):
    w = world
    assert say(w, "can we book a stay for my family", GUEST, reply("[no reply]")) == "answered"
    assert w.wa.texts()[-1].startswith("Sure, Kindly confirm the Location")
    assert say(w, "ok will book later", GUEST, reply("[no reply]")) == "quiet"


def test_naming_the_place_skips_the_location_question(world):
    w = world
    say(w, "rooms available in allepy?", GUEST)
    assert w.wa.texts() == [FORM] and w.repo.get_state(GUEST)["form_place"] == w.p1.id


def test_details_spread_over_messages_are_checked_in_code_not_asked_again(world):
    w = world
    say(w, "need a room in Alleppey", GUEST, tool("ask_stay_details", place="Alleppey"), reply("[no reply]"))
    say(w, "dec 12", GUEST)
    assert w.wa.texts() == [FORM, "Please share these details:\n\nCheck out :\nNumber of persons :"]  # only what is missing
    say(w, "3 persons, till 15", GUEST)
    assert w.wa.texts()[-2:] == ["Yes, rooms are available for your dates.",
                                 "Please share your name and mobile number to proceed."]
    assert w.repo.get_state(GUEST)["bk"]["check_out"] == "2026-12-15"


def test_a_number_after_rooms_are_found_books_even_if_the_ai_does_not(world):
    w = world
    say(w, "Alleppey 12 dec to 15 dec 2 people", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-12-12", check_out="2026-12-15", adults=2),
        reply("[no reply]"))
    say(w, "Rahul, 9876543210", GUEST, reply("The total is 24,000 rupees. Advance 12,000. Please share your name."))
    texts = w.wa.texts()
    assert texts[-2].startswith("Following are the glimpse of your enquiry") and "Guest - Rahul" in texts[-2]
    assert texts[-1].startswith("Please pay") and not any("24,000 rupees" in x for x in texts)


def test_no_rooms_for_the_dates_is_said_plainly(world):
    w = world
    for rt in w.repo.list_room_types(w.p1.id):
        w.repo.save_room_type(RoomType(**{**rt.__dict__, "units": 0}))
    say(w, "Alleppey 12 Dec to 15 Dec, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-12-12", check_out="2026-12-15", adults=2),
        reply("[no reply]"))
    assert w.wa.texts() == ["Sorry, rooms are not available for your dates."]


def test_the_bot_uses_whatever_the_dashboard_says_now(world):
    """Owner edits reach the bot on the next message: no training, no restart."""
    w = world
    w.repo.save_room_type(RoomType(**{**w.deluxe.__dict__, "base_rate": 5500}))
    w.repo.save_rate_rule(RateRule(w.deluxe.id, date(2026, 11, 10), date(2026, 11, 11), 7000, "Festival"))
    new = w.repo.save_property(Property("Misty Hills", city="Vagamon, Kerala", contact_numbers=["+919000000001"]))
    w.repo.save_room_type(RoomType(new.id, "Tent", units=4, base_rate=2000))
    say(w, "rooms 10 Nov 2 nights for 2", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=2, adults=2), reply("[no reply]"))
    deluxe = next(o for o in last_tool(w)["options"] if o["room_type"] == "Deluxe")
    assert deluxe["total_for_stay"] == 7000 + 7000  # the festival price on both nights
    system = w.llm.calls[-1].system
    assert "Misty Hills" in system and "Tent" in system and "Vagamon" in system  # a new property is known at once
    say(w, "resort", "+919847000777")
    assert "Vagamon" in w.wa.texts("+919847000777")[-1]  # and named in the location question


def test_a_town_answer_to_the_location_question_gets_the_form_without_the_ai(world):
    w = world
    say(w, "I need a resort", GUEST)
    calls = len(w.llm.calls)
    assert say(w, "Alleppey") == "form" and len(w.llm.calls) == calls  # no AI guesswork for this step
    assert w.wa.texts()[-1].startswith("Please share these details:")
    say(w, "20 Nov to 21 Nov, 2 persons", GUEST)  # checked in code, no AI (2026-11-19)
    assert w.repo.get_state(GUEST)["bk"]["check_in"] == "2026-11-20" and len(w.llm.calls) == calls
    say(w, "Kochi", "+919847000123")  # not after our location question: the AI handles it as usual
    assert w.llm.calls[-1].messages[-1].content.startswith("Kochi")


def test_a_mid_chat_hi_is_never_left_unanswered(world):
    w = world
    say(w, "Is there a beach view?", GUEST, reply("Yes sir, every room has a beach view."))
    assert say(w, "hi") == "greeting"
    assert w.wa.texts()[-1] == "Hi, How may I assist you?"
    assert say(w, "Hello") == "greeting" and w.wa.texts().count("Hi, How may I assist you?") == 2  # always answered


def test_no_message_is_ever_sent_twice_in_a_conversation(world):
    w = world
    line = "Every room has a beach view, sir."
    say(w, "Is there a beach view?", GUEST, reply(line))
    say(w, "hmm?", GUEST, reply(line))
    assert w.wa.texts().count(line) == 1
    w.clock.advance(minutes=60 * 25)  # a new conversation after a day of silence may say it again
    say(w, "Is there a beach view?", GUEST, reply(line))
    assert w.wa.texts().count(line) == 2


def test_an_essay_reply_gets_one_shorter_rewrite(world):
    w = world
    essay = " ".join(["Our resort is a lovely place by the backwaters with many things to enjoy."] * 4)
    say(w, "tell me about the place", GUEST, reply(essay), reply("Backwater view rooms, pool and a houseboat jetty."))
    assert w.wa.texts()[-1] == "Backwater view rooms, pool and a houseboat jetty."
    assert "Too long" in w.llm.calls[-1].messages[-1].content


def test_room_price_lines_do_not_count_as_chat_words():
    from app.resort.chat import MAX_CHAT_WORDS, chatty_words
    rooms = "\n".join(f"Deluxe room {i} with a lovely backwater view - ₹4,000 a night" for i in range(8))
    assert chatty_words(rooms + "\nWhich one would you like?") == 5 < MAX_CHAT_WORDS


def test_booking_chat_end_to_end_with_payment_link(world):
    w = world
    say(w, "Alleppey, 10 Nov 2 nights, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=2, adults=2, language="en"),
        reply("Rooms are available sir."))
    res = last_tool(w)
    assert res["place"] == "Pozhiyoram, Alleppey" and res["check_out"] == "2026-11-12"
    [deluxe] = res["options"]  # only the owner's top-priority room type is offered; the guest is never asked to pick
    assert deluxe["room_type"] == "Deluxe"
    assert deluxe["total_for_stay"] == 8000 and deluxe["advance_now"] == 4000 and deluxe["rooms_possible"] == [1, 2]
    assert w.wa.texts()[-1] == "Please share your name and mobile number to proceed."
    say(w, "1 room", GUEST, tool("prepare_booking", rooms=1), reply("Name and mobile please."))
    summary = last_tool(w)
    assert (summary["total"], summary["advance_now"], summary["balance_at_property"]) == (8000, 4000, 4000)
    assert w.repo.list_bookings(phone=GUEST) == []  # nothing booked yet
    assert confirm(w) == "payment_link"
    booking = w.repo.list_bookings(phone=GUEST)[0]
    assert (booking.status, booking.advance, booking.total, booking.rooms) == ("hold", 4000, 8000, 1)
    assert booking.guest_name == "Asha Menon" and w.pay.created[0]["amount"] == 4000
    texts = w.wa.texts()
    assert texts[-2].startswith("Following are the glimpse") and "Pay - 4,000" in texts[-2]
    assert texts[-1].startswith("Please pay") and "https://rzp.io/mock/" in texts[-1]
    assert {k for _, k, _ in w.wa.to(GUEST)} == {"text"}  # never a list or button
    assert "bk" not in w.repo.get_state(GUEST)


def test_the_bot_offers_only_the_owners_top_priority_room_type_and_never_asks_which(world):
    w = world
    w.repo.save_room_type(RoomType(**{**w.deluxe.__dict__, "priority": 2}))  # the owner puts Family Suite first
    say(w, "Alleppey, 10 Nov 2 nights, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=2, adults=2, language="en"),
        reply("[no reply]"))
    assert [o["room_type"] for o in last_tool(w)["options"]] == ["Family Suite"]
    say(w, "Asha, same number", GUEST, tool("create_booking", guest_name="Asha", mobile="same"), reply("[no reply]"))
    booking = w.repo.list_bookings(phone=GUEST)[0]
    assert booking.room_type_id == w.family.id
    assert "Never ask which room type" in w.llm.calls[0].system


def test_same_number_without_a_name_books_with_the_whatsapp_name_and_never_asks_to_pick_a_room(world):
    w = world
    w.repo.save_room_type(RoomType(**{**w.family.__dict__, "units": 0}))  # Family Suite is full
    say(w, "Alleppey, 10 Nov 2 nights, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=2, adults=2, language="en"),
        reply("[no reply]"))
    m = InboundMessage(f"wamid.{next(_ids)}", GUEST, "text", datetime(2026, 11, 2, 4, 31), text="same number",
                       profile_name="Rahul K")
    w.llm.script.extend([tool("create_booking", guest_name="", mobile="same", room_type="Family Suite"),
                         reply("[no reply]")])
    w.bot.handle(m)
    assert last_tool(w)["ok"] is True
    booking = w.repo.list_bookings(phone=GUEST)[0]
    assert booking.guest_name == "Rahul K" and booking.room_type_id == w.deluxe.id  # the AI's guessed room is ignored
    texts = w.wa.texts()
    assert texts[-1].startswith("Please pay") and "Which one" not in " ".join(texts)


def test_no_name_anywhere_asks_only_for_the_name_and_keeps_the_number(world):
    w = world
    say(w, "Alleppey, 10 Nov 2 nights, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=2, adults=2, language="en"),
        reply("[no reply]"))
    say(w, "same number", GUEST, tool("create_booking", guest_name="", mobile="same"), reply("Your name please sir?"))
    assert "ask ONLY the guest's name" in last_tool(w)["error"]
    say(w, "Asha", GUEST, tool("create_booking", guest_name="Asha", mobile=""), reply("[no reply]"))
    booking = w.repo.list_bookings(phone=GUEST)[0]
    assert booking.guest_name == "Asha" and "Mobile:" in booking.notes


def test_system_prompt_has_the_rules_and_facts(world):
    w = world
    say(w, "do you allow pets?", GUEST, reply("Sorry, I am not sure about pets."))
    system = w.llm.calls[0].system
    assert "9497861924" in system and "9995855789" in system and "https://maps.app.goo.gl/abc" in system
    assert "Never ask which language" in system and "Manglish" in system and "Hinglish" in system
    assert "the owner does it" in system and "NEGOTIATION" in system and "Never offer a" in system
    assert "₹4,000" not in system and "Extra bed: ₹500" in system  # prices only ever come from the tools
    assert "Kanthalloor, Munnar" in system and "2026-11-02 (Monday)" in system


def test_a_decided_guest_gets_the_payment_link_in_the_same_turn_without_a_second_confirmation(world):
    w = world
    say(w, "book deluxe 10 Nov 2 nights for 2", GUEST,
        tool("check_availability", place="alleppey", check_in="2026-11-10", nights=2, adults=2),
        tool("prepare_booking", room_type="Deluxe", rooms=1),
        tool("create_booking", guest_name="Asha", mobile="same"),
        reply("[no reply]"))
    assert last_tool(w)["ok"] is True
    assert len(w.repo.list_bookings(phone=GUEST)) == 1 and len(w.pay.created) == 1
    assert "https://rzp.io/mock/" in w.wa.texts()[-1]


def test_create_needs_a_prepared_booking_and_a_name(world):
    w = world
    say(w, "yes book it", GUEST, tool("create_booking", guest_name="Asha"), reply("Which dates?"))
    assert "check_availability" in last_tool(w)["error"]
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
    assert "price_per_night" not in deluxe and "rate_per_room_per_night" not in deluxe  # nights differ: only the total is given
    assert "Never mention that rates change" in last_tool(w)["note"]
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
    say(w, "munnar 2 people", GUEST,
        tool("check_availability", place="Munnar", check_in="2026-11-10", nights=1, adults=2), reply("ok"))
    assert last_tool(w)["place"] == "Kanthalloor, Munnar"
    say(w, "goa 2 people", GUEST,
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
    say(w, "yes, Asha", GUEST, tool("create_booking", guest_name="Asha", mobile="same"), reply("Sorry, those rooms just went."))
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
    say(w, "yes, Asha", GUEST, tool("create_booking", guest_name="Asha", mobile="same"), reply("never shown"))
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
        assert {"en", "ml", "ta", "hi"} <= set(table) <= {"en", "ml", "ta", "hi", "mn", "tn", "hn"}, key
        assert all(v.strip() for v in table.values()), key
    assert t("greet", "xx") == "Hi, How may I assist you?"  # unknown language: English


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


def _echo(text, phone="919847012345", mid="e1"):
    return parse_echoes({"entry": [{"changes": [{"field": "smb_message_echoes", "value": {
        "metadata": {"phone_number_id": "123"},
        "message_echoes": [{"from": "914800000000", "to": phone, "id": mid, "timestamp": "1790000000",
                            "type": "text", "text": {"body": text}}]}}]}]}, "123")


def test_human_reply_in_phone_app_pauses_bot(world):
    w = world
    (echo,) = _echo("Sir, room confirm cheyyam", GUEST.lstrip("+"))
    assert echo.phone == GUEST
    assert w.bot.human_replied(echo) == "paused_by_human"
    assert w.repo.is_paused(GUEST)[0]
    assert w.repo.list_chat(GUEST)[-1].source == "staff"
    before = len(w.wa.to(GUEST))
    assert say(w, "ok thanks, what time?") == "paused"
    assert len(w.wa.to(GUEST)) == before
    assert w.bot.human_replied(echo) == "duplicate"  # Meta may resend
    w.clock.advance(hours=25)  # staff went quiet: the bot is back
    assert say(w, "hello again", GUEST, reply("Hi, how can I help?")) != "paused"


def test_echo_of_a_bot_message_does_not_pause(world):
    w = world
    w.bot._text(GUEST, "Hello from the bot")
    (echo,) = _echo("Hello from the bot", GUEST.lstrip("+"), "e2")
    assert w.bot.human_replied(echo) == "bot_echo"
    assert not w.repo.is_paused(GUEST)[0]


def test_stop_and_start(world):
    w = world
    assert say(w, "STOP") == "opt_out"
    assert w.repo.get_guest(GUEST).offers_consent is False
    assert say(w, "START") == "opt_in"


def test_after_payment_confirmation_then_policy_then_stay_details_and_no_invoice(world):
    w = world
    w.repo.save_property(Property(**{**w.repo.get_property(w.p1.id).__dict__,
                                     "stay_info": "Location 👇\nhttps://maps.app.goo.gl/x\nWifi - pass"}))
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    before = len(w.wa.to(GUEST))
    w.bot.mark_paid(booking_id=b.id)
    sent = w.wa.to(GUEST)[before:]
    assert [k for _, k, _ in sent] == ["text", "text", "text"]  # no PDF document
    assert sent[0][2].endswith("*Booking Confirmed ✅*") and sent[1][2] == "Free cancellation 7 days before."
    assert sent[2][2].startswith("Location") and "Wifi" in sent[2][2]


def test_after_not_available_the_guest_writing_on_gets_the_form_for_other_dates(world):
    w = world
    for rt in w.repo.list_room_types(w.p1.id):
        w.repo.save_room_type(RoomType(**{**rt.__dict__, "units": 0}))
    say(w, "Alleppey 12 dec to 14 dec 2 people", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-12-12", check_out="2026-12-14", adults=2),
        reply("[no reply]"))
    assert w.wa.texts()[-1] == "Sorry, rooms are not available for your dates."
    say(w, "Rahul, 9876543210", GUEST, reply("[no reply]"))  # never silence (stress test, 2026-11-18)
    assert w.wa.texts()[-1].startswith("Please share these details")


@pytest.mark.parametrize("answer", ["Kanthalloor", "kanthaloor", "munnar"])
def test_a_resort_switched_off_in_the_dashboard_gets_the_phone_numbers_not_a_re_ask(world, answer):
    w = world
    w.repo.save_property(Property(**{**w.p2.__dict__, "active": False, "contact_numbers": ["+919497861924"]}))
    assert say(w, "Rooms?") == "location"
    assert say(w, answer) == "closed_place"
    assert w.wa.texts()[-1] == "Sorry, Munnar is currently not available."


def test_a_status_question_gets_no_unasked_money_talk(world):
    w = world
    hold(w, rooms=1)
    w.bot.mark_paid(booking_id=w.repo.list_bookings(phone=GUEST)[0].id)
    say(w, "Booking confirm alle", GUEST, tool("my_bookings"),
        reply("Sir, booking confirmed aanu. Balance 4000 property il kodukkanam. Vere enthenkilum ariyanamundo?"))
    assert w.wa.texts()[-1] == "Sir, booking confirmed aanu."


def test_talk_to_owner_pauses_the_bot_and_sends_the_owner_a_summary(world):
    w = world
    say(w, "I want to talk to the owner", GUEST, tool("talk_to_owner", reason="guest asked for the owner"),
        reply("[no reply]"))
    assert w.wa.texts()[-1] == "Thank you. Our team will message you here shortly."
    staff = w.wa.texts(STAFF)[-1]
    assert staff.startswith("Guest needs the team: guest asked for the owner") and GUEST in staff
    calls = len(w.llm.calls)
    say(w, "hello?")  # paused: the owner talks now, the bot stays out
    assert len(w.llm.calls) == calls and w.wa.texts()[-1] == "Thank you. Our team will message you here shortly."


def test_a_cancel_request_gets_the_policy_charge_from_code(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.bot.mark_paid(booking_id=b.id)
    say(w, "I want to cancel my booking", GUEST, tool("my_bookings"), reply("[no reply]"))
    row = last_tool(w)["bookings"][0]
    assert row["if_cancelled_today"]["retention_pct"] == 10  # 8 days before check-in: 10% of the total kept
    assert "cancel" in w.wa.texts(STAFF)[-1]  # the owner is alerted at once


def test_a_paid_guest_saying_hi_is_never_told_to_complete_the_payment(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.bot.mark_paid(booking_id=b.id)
    w.clock.advance(hours=15)  # the next morning, same conversation (the real case on 2026-11-18)
    calls = len(w.llm.calls)
    assert say(w, "Hi") == "greeting"
    assert w.wa.texts()[-1] == "Hi, How may I assist you?"
    assert len(w.llm.calls) == calls  # no AI guess for a plain hi
    say(w, "is there parking?", GUEST,
        reply("Yes, parking is free. Please complete the payment."))
    assert w.wa.texts()[-1] == "Yes, parking is free."  # the stale payment talk is dropped


def test_a_new_name_after_restart_wins_and_a_15_digit_number_is_refused(world):
    w = world
    w.repo.touch_guest(GUEST, w.clock.now(), name="John")  # saved from an earlier chat
    say(w, "Alleppey, 12 dec to 14 dec, 2 people", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-12-12", check_out="2026-12-14", adults=2),
        reply("[no reply]"))
    say(w, "Shibil 859002727", GUEST, reply("That mobile number does not look right. Please send it again."))
    assert w.repo.list_bookings(phone=GUEST) == []  # 9 digits: no booking
    say(w, "637737289202029", GUEST, reply("Please send a 10 digit mobile number."))
    assert w.repo.list_bookings(phone=GUEST) == []  # 15 digits: never the last 10 of it
    say(w, "9846012345", GUEST, reply("[no reply]"))
    summary = next(x for x in w.wa.texts() if x.startswith("Following are the glimpse of your enquiry"))
    assert "Guest - Shibil" in summary and "98460 12345" in summary and "Pay - " in summary and "Ie - " not in summary


def test_a_badly_spelt_or_unclear_location_answer_is_never_left_silent(world):
    w = world
    assert say(w, "Rooms?") == "location"
    assert say(w, "alloy") == "form" and w.repo.get_state(GUEST)["form_place"] == w.p1.id  # 'all...' = Alleppey
    assert say(w, "restart") == "restart"
    say(w, "Rooms?")
    towns = " or ".join(w.bot._towns())
    assert say(w, "xyz") == "location" and w.wa.texts()[-1] == f"{towns} ?"  # not understood: asked again, short
    assert say(w, "kanth") == "form" and w.repo.get_state(GUEST)["form_place"] == w.p2.id


@pytest.mark.parametrize("wish", ["Rooms?", "room venam", "need a room", "resort booking", "any rooms available?",
                                  "Room chahiye", "rooms irukka"])
def test_a_short_booking_wish_always_gets_the_location_question_in_code(world, wish):
    w = world
    hold(w, rooms=1)  # an old booking chat full of summary and payment talk (the real case, 2026-11-18)
    calls = len(w.llm.calls)
    assert say(w, wish) == "location"
    assert w.wa.texts()[-1].startswith("Sure, Kindly confirm the Location") and len(w.llm.calls) == calls


def test_a_room_question_still_goes_to_the_ai(world):
    w = world
    assert say(w, "rooms have AC?", GUEST, reply("Yes, all rooms have AC.")) == "answered"
    assert say(w, "need a room in allepy", GUEST) == "form"  # a named place goes straight to the form


def test_a_confirmed_booking_closes_the_chat_and_the_next_message_starts_fresh(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.bot.mark_paid(booking_id=b.id)
    w.bot._log(GUEST, "[invoice] Invoice-INV-2026-X.pdf")  # an old log row (the invoice feature is gone; old chats keep such rows)
    first = len(w.llm.calls)
    say(w, "is there parking?", GUEST, reply("Your booking is on hold. [invoice] Invoice-INV-2026-X.pdf"))
    seen = "\n".join(m.content for m in w.llm.calls[first].messages)  # what the AI was shown this turn
    assert "Following are the glimpse" not in seen and "[invoice]" not in seen  # the old booking chat is closed
    assert "[invoice]" not in w.wa.texts()[-1] and ".pdf" not in w.wa.texts()[-1]
    assert "confirmed" in w.llm.calls[-1].system  # the booking itself is still in the guest's records


def test_after_payment_the_guest_gets_only_the_clients_confirmation_with_the_room_number(world):
    w = world
    w.repo.save_room_type(RoomType(**{**w.deluxe.__dict__, "room_list": ["104", "105"]}))
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    before = len(w.wa.to(GUEST))
    w.bot.mark_paid(booking_id=b.id)
    sent = w.wa.to(GUEST)[before:]
    # the confirmation, then the cancellation policy (owner, 2026-11-18); no invoice, no map line, no ID request
    assert [k for _, k, _ in sent] == ["text", "text"] and sent[1][2] == "Free cancellation 7 days before."
    w.bot.announce_paid(w.repo.get_booking(b.id))  # announced again by staff
    assert [k for _, k, _ in w.wa.to(GUEST)].count("document") == 0  # no invoice is ever sent (owner, 2026-11-18)
    assert sent[0][2] == "\n".join([
        "Following are the glimpse of your booking,", "",
        "Guest - Asha Menon", "98470 12345",
        "Check in - 10th Nov 26", "Check out - 12th Nov 26", "2 Nights", "2 PAX", "1 Room",
        "*#104*", "*Deluxe*",
        "Room Cost - 8,000", "Total Cost - 8,000", "",
        "Advance Paid - 4,000", "Balance Payable - 4,000", "*Booking Confirmed ✅*"])


# --------------------------------------------------------------------------- payments

def test_paid_event_confirms_booking_and_alerts_staff(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    assert w.bot.mark_paid(link_id=b.payment_link_id, payment_id="pay_1") == "paid"
    b = w.repo.get_booking(b.id)
    assert b.status == "paid" and b.razorpay_payment_id == "pay_1" and b.paid_at
    confirmed = next(x for x in w.wa.texts() if x.endswith("*Booking Confirmed ✅*"))
    assert w.wa.texts()[-1] == "Free cancellation 7 days before."  # the policy follows; no ID request after it
    assert w.wa.texts().index(confirmed) == len(w.wa.texts()) - 2
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
    web.wait_idle()  # handled on the bot's own threads, after Meta got its 200
    assert w.wa.last()[1] == "text" and w.wa.last()[2] == "Hi, How may I assist you?"


def test_the_webhook_answers_meta_at_once_and_never_waits_for_a_slow_guest_turn(world, monkeypatch):
    """Stress test (2026-11-18): guest turns run on the bot's own threads, so a slow AI turn never holds up Meta's
    200 or the shared threads behind /health and the dashboard."""
    import threading
    import time
    monkeypatch.setenv("RESORT_WHATSAPP_APP_SECRET", "appsecret")
    monkeypatch.setenv("RESORT_WHATSAPP_PHONE_NUMBER_ID", "123")
    w = world
    release, started = threading.Event(), threading.Event()

    def slow(msg):
        started.set()
        release.wait(5)
        return "answered"

    services = ResortServices(w.bot.config, w.repo, w.wa, w.pay, w.clock, None, _llm=w.llm)
    services.__dict__["bot"] = type("B", (), {"handle": staticmethod(slow)})()
    app = FastAPI()
    app.state.resort = services
    app.include_router(web.router)
    body = json.dumps({"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "123"}, "messages": [{"from": "919847012345", "id": "wamid.slow1",
                                                              "timestamp": "1790000000", "type": "text",
                                                              "text": {"body": "room?"}}]}}]}]}).encode()
    t0 = time.monotonic()
    resp = TestClient(app).post("/resort/webhook", content=body,
                                headers={"x-hub-signature-256": "sha256=" + _signed(body, "appsecret")})
    assert resp.status_code == 200 and time.monotonic() - t0 < 2  # Meta is answered while the turn still runs
    assert started.wait(2)
    release.set()
    web.wait_idle()


def test_a_guest_who_loses_the_last_room_to_someone_else_is_told_it_is_not_available(world):
    """Stress test (2026-11-18): with many guests at once, more were told 'available' than there were rooms; the one
    whose name came in after the last room went got silence. Now the fixed 'not available' line goes."""
    w = world
    for rt in w.repo.list_room_types(w.p1.id):
        if rt.name != "Deluxe":
            w.repo.save_room_type(RoomType(**{**rt.__dict__, "active": False}))
    say(w, "Alleppey 12 dec to 14 dec 2 people", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-12-12", check_out="2026-12-14", adults=2),
        reply("[no reply]"))
    for i in range(2):  # meanwhile two other guests take both Deluxe rooms
        w.repo.create_hold(Booking(w.p1.id, w.deluxe.id, f"+91981111111{i}", date(2026, 12, 12), date(2026, 12, 14),
                                   2, 0, 1, 8000, 4000, w.clock.now(),
                                   hold_expires_at=w.clock.now() + timedelta(minutes=15)), w.clock.now())
    say(w, "Asha, 9847012345", GUEST, tool("create_booking", guest_name="Asha", mobile="9847012345"), reply("[no reply]"))
    assert w.wa.texts()[-1] == "Sorry, rooms are not available for your dates."
    assert w.repo.list_bookings(phone=GUEST) == []


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
    assert b.payment_link_url in last and last.startswith("Please pay")


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
    say(w, "yes, Asha Menon from Kochi", GUEST, tool("create_booking", guest_name="Asha Menon", mobile="same", from_city="Kochi"),
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
    say(w, "Asha, 9847012345", GUEST, tool("create_booking", guest_name="Asha", mobile="9847012345", language="manglish"),
        reply("[no reply]"))
    pay_text = w.wa.texts()[-1]
    assert pay_text.startswith("Ee link vazhi advance pay cheyyuka") and w.repo.get_state(GUEST)["lang"] == "mn"
    for banned in ("held", "booked", "reserved"):
        assert banned not in pay_text.lower()
    assert w.wa.texts()[-2].startswith("Following are the glimpse")  # the summary is always English


def test_english_payment_message_only_asks_to_pay(world):
    w = world
    hold(w, rooms=1)
    text = w.wa.texts()[-1]
    assert text.startswith("Please pay the advance using this link:\nhttps://") and "%" not in text
    for banned in ("held", "is booked", "reserved"):
        assert banned not in text.lower()


def test_every_greeting_gets_the_same_line_with_one_place_open(world):
    w = world
    w.repo.save_property(Property(**{**w.p2.__dict__, "active": False}))  # Kanthalloor switched off
    assert say(w, "hi") == "greeting"
    assert w.wa.texts()[-1] == "Hi, How may I assist you?"
    say(w, "Hello", "+919847000011")
    assert w.wa.texts("+919847000011")[-1] == "Hi, How may I assist you?"
    w.repo.set_state("+919847000012", {"lang": "mn"})
    say(w, "hi", "+919847000012")
    assert w.wa.texts("+919847000012")[-1] == "Hi, How may I assist you?"
    assert w.llm.calls == []  # greetings never cost an AI call


def test_manglish_guest_never_gets_malayalam_script(world):
    w = world
    say(w, "avde engane und", GUEST, reply("അവിടെ നല്ലതാണ്, ബീച്ച് ഉണ്ട്"), reply("Avde nalla rasam und bro, beach um und."))
    assert w.wa.texts() == ["Avde nalla rasam und bro, beach um und."]
    assert "Rewrite that reply using only English letters" in w.llm.calls[1].messages[-1].content


def test_malayalam_script_guest_is_not_rewritten(world):
    w = world
    say(w, "അവിടെ എങ്ങനെയുണ്ട്", GUEST, reply("Avide nallathaanu, beach und"))
    assert w.wa.texts() == ["Avide nallathaanu, beach und"] and len(w.llm.calls) == 1
    assert "Manglish" in w.llm.calls[0].system.split("REPLY LANGUAGE (")[1]


def test_a_native_script_reply_is_rewritten_to_english_letters_for_a_script_guest(world):
    w = world
    say(w, "അവിടെ എങ്ങനെയുണ്ട്", GUEST, reply("അവിടെ നല്ലതാണ്"), reply("Avide nallathaanu"))
    assert w.wa.texts() == ["Avide nallathaanu"]
    assert "only English letters (Manglish)" in w.llm.calls[1].messages[-1].content


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
    assert say(w, "ok", GUEST, tool("create_booking", guest_name="Asha", mobile="same"),
               reply("[no reply]")) == "payment_link"


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
    say(w, "I need some information", GUEST, reply("Sure, what would you like to know?"))
    system = w.llm.calls[0].system
    assert "ONE language per reply" in system and "NEVER in Malayalam script" in system
    assert "sent automatically, always in English" in system
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
    assert any("Booking Confirmed" in text for text in w.wa.texts())
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
    assert w.repo.get_booking(b.id).status == "paid" and any("Booking Confirmed" in t for t in new)
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


def test_casual_words_get_no_reply_but_real_talk_does(world):
    w = world
    say(w, "Alleppey il beach undo?", GUEST, reply("Und bro."))
    say(w, "2 per, 10 nu", GUEST, tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=1, adults=2,
                                       language="manglish"), reply("Room und."))
    say(w, "beach adutha?", GUEST, reply("Athe, beach view aanu."))
    assert w.wa.texts()[-1].endswith(".")  # a statement: nothing is being asked now
    assert say(w, "ok") == "next_step"  # 'ok' mid-chat carries on to the open step (owner, 2026-11-18)
    assert w.wa.texts()[-1] == "Interested aanenkil ningalude name-um mobile number-um ayakkamo?"
    before, calls = len(w.wa.to(GUEST)), len(w.llm.calls)
    for casual in ("ok", "thanks a lot", "nice", "adipoli", "haha 😄", "okay then", "ok will see", "super bro", "kollam", "👍"):
        assert say(w, casual) == "quiet", casual
    assert len(w.wa.to(GUEST)) == before and len(w.llm.calls) == calls
    # anything with real content goes to the AI, even when it starts with "ok"
    assert say(w, "ok how much for 3 nights?", GUEST, reply("3 nights is ₹12,000.")) == "answered"
    assert say(w, "ok book it", GUEST, reply("Sure, which room type?")) == "answered"


def test_the_prompt_limits_replies_to_necessary_talk(world):
    w = world
    say(w, "what is the price?", GUEST, reply("Which dates?"))
    system = w.llm.calls[0].system
    assert "Reply only when it is necessary" in system and "will tell my family" in system and "[no reply]" in system


# --------------------------------------------------------------------------- giving the guest time to finish


def test_unfinished_messages_are_recognised():
    from app.resort.chat import looks_unfinished
    for open_ended in ("njan udheshikkunath oru", "we are planning to", "for 3 people,", "ente friends um...", "enikku", "I want a"):
        assert looks_unfinished(open_ended), open_ended
    for finished in ("25 dec aanu", "2 per", "Alleppey", "avde engane und", "how many rooms?", "ok", "10 nu varum.", "great!"):
        assert not looks_unfinished(finished), finished


def test_the_bot_waits_longer_for_a_cut_off_message_then_answers_both_together(world, monkeypatch):
    from app.resort import chat
    w = world
    w.bot.debounce_s, w.bot.patience_s = 2.5, 8.0
    waits = []

    def sleeper(seconds):
        waits.append(seconds)
        if len(waits) == 1:  # while we wait, the guest finishes the thought in a second message
            w.bot.debounce_s = w.bot.patience_s = 0
            say(w, "25 dec aanu", GUEST, reply("25 Dec il Alleppey aano Kanthalloor aano nokkunnath."))

    monkeypatch.setattr(chat.time, "sleep", sleeper)
    assert say(w, "njan udheshikkunath oru") == "superseded"
    assert waits == [8.0]  # the long wait, because the first message stopped mid-sentence
    assert w.wa.texts() == ["25 Dec il Alleppey aano Kanthalloor aano nokkunnath."]  # answered once
    assert w.llm.calls[0].messages[-1].content == "njan udheshikkunath oru\n25 dec aanu"


def test_a_complete_message_waits_only_the_short_time(world, monkeypatch):
    from app.resort import chat
    w = world
    w.bot.debounce_s, w.bot.patience_s = 2.5, 8.0
    waits = []
    monkeypatch.setattr(chat.time, "sleep", waits.append)
    say(w, "25 dec aanu", GUEST, reply("Ethra divasam?"))
    assert waits == [2.5]


def test_if_nothing_follows_a_cut_off_message_the_bot_asks_them_to_go_on(world):
    w = world
    say(w, "njan udheshikkunath oru", GUEST, reply("Sheri bro, parayu, entha plan?"))
    assert w.wa.texts() == ["Sheri bro, parayu, entha plan?"]
    assert "cut off or you cannot tell what they mean" in w.llm.calls[0].system


# --------------------------------------------------------------------------- "sir", and an offers question is not a haggle


def test_the_prompt_says_sir_and_treats_an_offers_question_as_a_plain_question(world):
    w = world
    say(w, "offer undo?", GUEST, reply("Which dates, sir?"))
    system = w.llm.calls[0].system
    assert 'Say "sir" at most ONCE' in system and "Never bro" in system and "Und bro" not in system
    assert "Asking about offers, deals or the price is only a question, NOT a request to reduce" in system
    assert "Never say there is no offer, never say you cannot reduce" in system
    assert "Only when the guest asks for a lower price" in system


# --------------------------------------------------------------------------- the summary, and never a price-hike story


def test_the_summary_is_built_in_english_laid_out_line_by_line_and_sent_before_the_ai_line(world):
    w = world
    say(w, "Alleppey il 10 nu 2 divasam, 2 per", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=2, adults=2, language="manglish"),
        reply("Deluxe 8000 aanu sir."))
    w.repo.save_property(Property(**{**w.repo.get_property(w.p1.id).__dict__, "meal_plan": "Stay Only",
                                     "early_checkin_time": "06:00", "early_checkin_rate": 1000}))
    say(w, "deluxe mathi, early check-in venam", GUEST,
        tool("prepare_booking", room_type="Deluxe", rooms=1, early_checkin=True, language="manglish"),
        reply("Name-um mobile number-um parayamo?"))
    # the fixed ask already went with the glimpse: the AI's own repeat of it is not sent (nothing twice, 2026-11-18)
    assert w.wa.texts()[-1] == "Interested aanenkil ningalude name-um mobile number-um ayakkamo?"
    assert w.repo.list_bookings(phone=GUEST) == []
    say(w, "Kavana Priya, 80886 78167", GUEST,
        tool("create_booking", guest_name="Kavana Priya", mobile="80886 78167", language="manglish"), reply("[no reply]"))
    texts = w.wa.texts()
    assert texts[-2] == "\n".join([
        "Following are the glimpse of your enquiry,", "",
        "Guest - Kavana Priya",
        "80886 78167",
        "Check in - 10th Nov 26",
        "*Early Check-in @ 6am*",
        "Check out - 12th Nov 26",
        "2 Nights",
        "2 PAX",
        "1 Room",
        "*Deluxe*",
        "*Stay Only*",
        "Room Cost - 8,000",
        "Early Check-in Charges - 1,000",
        "Total Cost - 9,000", "",
        "As a procedure you may need to pay an advance to block the room.", "",
        "Pay - 4,500", "",
        "Balance can be paid during the time of check in."])
    assert texts[-1].startswith("Please pay") or texts[-1].startswith("Ee link")
    b = w.repo.list_bookings(phone=GUEST)[0]
    assert (b.total, b.advance) == (9000, 4500) and "Mobile: 8088678167" in b.notes and "Early check-in" in b.notes
    assert w.pay.created[0]["amount"] == 4500


def test_extra_bed_shows_in_the_summary(world):
    w = world
    to_summary(w, beds=1, adults=2)
    confirm(w)
    summary = next(t for t in w.wa.texts() if t.startswith("Following are the glimpse"))
    assert "Extra Bed - 1,000" in summary and "2 PAX" in summary and "Total Cost - 9,000" in summary


def test_the_form_then_glimpse_then_ask_name_in_the_clients_order(world):
    w = world
    for p in (w.p1, w.p2):
        w.repo.save_property(Property(**{**w.repo.get_property(p.id).__dict__, "active": p is w.p1}))
    w.repo.save_property(Property(**{**w.repo.get_property(w.p1.id).__dict__,
                                     "intro": "Your Beachfront Hideout Awaits!\nRoom - {price}/Night/Room"}))
    for rt in w.repo.list_room_types(w.p1.id):  # one room type, like Pozhiyoram
        if rt.name != "Deluxe":
            w.repo.save_room_type(RoomType(**{**rt.__dict__, "active": False}))
    say(w, "Pozhiyoram", GUEST, tool("ask_stay_details", place="Pozhiyoram"), reply("[no reply]"))
    assert w.wa.texts()[-1] == "Please share these details:\n\nCheck in :\nCheck out :\nNumber of persons :"
    say(w, "Check in : 10 Nov\nCheck out : 12 Nov\nNumber of persons : 2", GUEST,
        tool("check_availability", place="Pozhiyoram", check_in="2026-11-10", check_out="2026-11-12", adults=2),
        reply("Rooms are available sir. Beach View AC Room - 3000 per night, total 6000. Name and mobile please."))
    assert "3000" not in " ".join(w.wa.texts())  # the AI's own availability line (with prices) is never sent
    assert w.wa.texts()[-3:] == ["Yes, rooms are available for your dates.",
                                 "Your Beachfront Hideout Awaits!\nRoom - ₹4,000/Night/Room",
                                 "Please share your name and mobile number to proceed."]
    say(w, "Asha, same number", GUEST, tool("create_booking", guest_name="Asha", mobile="same"), reply("[no reply]"))
    texts = w.wa.texts()
    assert texts[-2].startswith("Following are the glimpse") and "*Deluxe*" not in texts[-2]  # one room type: no name
    assert texts[-1].startswith("Please pay") and "9847012345"[:5] in texts[-2]
    say(w, "dates again 1 Dec 1 night 2", GUEST,
        tool("check_availability", place="Pozhiyoram", check_in="2026-12-01", nights=1, adults=2), reply("Yes, available."))
    assert not any("Beachfront" in x for x in w.wa.texts()[-3:])  # the glimpse goes once
    say(w, "Asha, 9847012345, early check in?", GUEST,
        tool("check_availability", place="Pozhiyoram", check_in="2026-12-01", nights=1, adults=2),
        tool("create_booking", guest_name="Asha", mobile="9847012345"), reply("[no reply]"))
    assert "Please share your name" not in " ".join(w.wa.texts()[-2:])  # not asked again with the summary


def test_a_per_night_price_is_given_only_when_every_night_costs_the_same(world):
    w = world
    say(w, "2 nights from 10 Nov", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", nights=2, adults=2), reply("ok"))
    deluxe = next(o for o in last_tool(w)["options"] if o["room_type"] == "Deluxe")
    assert deluxe["price_per_night"] == 4000 and deluxe["total_for_stay"] == 8000


def test_the_prompt_hides_rates_and_forbids_a_price_hike_story(world):
    w = world
    say(w, "price entha?", GUEST, reply("Dates parayamo sir?"))
    system = w.llm.calls[0].system
    assert "rate" not in system.lower().split("facts")[-1].replace("rated", "")  # no rate table among the facts
    assert "Never tell the guest that rates change" in system and "peak or high season" in system
    assert "Never quote a price from memory" in system
    assert "NO essays" in system and "Never list room types" in system


# --------------------------------------------------------------------------- found by playing the bot against the real AI


def test_a_date_with_a_year_that_has_passed_rolls_to_the_next_time_it_comes(world):
    w = world  # today in these tests is 2 Nov 2026
    say(w, "25 December, 2 nights", GUEST,
        tool("check_availability", place="Alleppey", check_in="2025-12-25", nights=2, adults=2), reply("ok"))
    res = last_tool(w)
    assert (res["check_in"], res["check_out"]) == ("2026-12-25", "2026-12-27") and "error" not in res
    assert "taken as 2026-12-25 to 2026-12-27" in res["note"]
    assert w.repo.get_state(GUEST)["bk"]["check_in"] == "2026-12-25"


def test_yesterday_is_still_refused_not_moved_a_year(world):
    w = world
    say(w, "yesterday", GUEST, tool("check_availability", place="Alleppey", check_in="2026-11-01", nights=1, adults=2), reply("?"))
    assert "past" in last_tool(w)["error"]


def test_the_guest_is_never_called_by_name(world):
    from app.resort.chat import ChatMixin
    clean = ChatMixin._without_name
    assert clean("Please confirm, Rahul sir.", "Rahul Menon") == "Please confirm, sir."
    assert clean("Sure, sir Rahul, one moment", "Rahul") == "Sure, sir, one moment"
    assert clean("Thank you, Rahul. Sir, the link follows.", "Rahul") == "Thank you. Sir, the link follows."
    assert clean("Hello sir", "") == "Hello sir" and clean("Rahul is the owner", "") == "Rahul is the owner"
    w = world
    w.repo.touch_guest(GUEST, w.clock.now(), name="Rahul")
    say(w, "is there parking?", GUEST, reply("Yes Rahul sir, parking is free."))
    assert w.wa.texts()[-1] == "Yes sir, parking is free."


def test_the_guests_own_script_decides_the_fixed_language_whatever_the_ai_calls_it(world):
    w = world
    say(w, "ஆலப்புழாவில் ரூம் இருக்கா? டிசம்பர் 25, 2 பேர்", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-12-25", nights=2, adults=2, language="ml"),
        reply("கிடைக்கும் sir."))
    assert w.repo.get_state(GUEST)["lang"] == "ta" and w.repo.get_state(GUEST)["style"] == "tanglish"
    say(w, "ok", "+919847000021", tool("check_availability", place="Alleppey", check_in="2026-12-25", nights=2,
                                       adults=2, language="ml"), reply("ok"))  # neutral first word, AI says script
    assert w.repo.get_state("+919847000021")["lang"] == "mn"  # an English-letters guest never gets script messages


def test_a_foreign_script_inside_a_reply_gets_one_rewrite(world):
    w = world
    say(w, "ஆலப்புழாவில் ரூம் இருக்கா?", GUEST, reply("அതെ sir, ரூம் இருக்கு."), reply("Aama sir, room irukku."))
    assert w.wa.texts() == ["Aama sir, room irukku."]
    assert "only English letters (Tanglish)" in w.llm.calls[1].messages[-1].content
    assert "Do not use Malayalam, Tamil or Hindi script" in w.llm.calls[1].messages[-1].content


def test_a_plain_offers_question_never_gets_a_no_offer_answer(world):
    w = world
    say(w, "offer undo?", GUEST, reply("Sorry sir, no offers right now."), reply("Sir, beach view room 3000 aanu."))
    assert w.wa.texts() == ["Sir, beach view room 3000 aanu."]
    assert "without saying there is no offer" in w.llm.calls[1].messages[-1].content
    say(w, "can you reduce the price a little?", GUEST, reply("Sorry sir, I cannot reduce it."),
        reply("Sir, you get a beachfront room with breakfast at this price; guests love it."))
    assert w.wa.texts()[-1].startswith("You get a beachfront")  # a haggle is convinced, never refused
    assert "convince the guest" in w.llm.calls[3].messages[-1].content


def test_sir_is_said_only_once_in_a_conversation(world):
    w = world
    say(w, "beach und?", GUEST, reply("Sir, und. Beach thottu aanu."))
    say(w, "pool?", GUEST, reply("Sir, pool illa."))
    say(w, "food?", GUEST, reply("Sheri sir, Swiggy, Zomato varum. Ok sir."))
    say(w, "parking?", GUEST, reply("Parking und. Thank you sir!"))
    assert w.wa.texts() == ["Sir, und. Beach thottu aanu.", "Pool illa.", "Sheri, Swiggy, Zomato varum. Ok.",
                            "Parking und. Thank you!"]  # client, 2026-11-17: sir once only


def test_the_guest_is_never_called_by_name_in_any_script():
    from app.resort.chat import ChatMixin
    clean = ChatMixin._without_name
    assert clean("അനു sir, ശരി", "അനു Menon") == "sir, ശരി"
    assert clean("நன்றி, கார்த்திக் sir", "கார்த்திக்") == "நன்றி, sir"
    assert clean("ठीक है, राहुल, link भेज रहा हूँ", "राहुल") == "ठीक है, link भेज रहा हूँ"


def test_the_prompt_never_lets_the_ai_assume_the_guest_count(world):
    w = world
    say(w, "is it near the beach?", GUEST, reply("Yes."))
    assert "Never assume the number of persons or nights" in w.llm.calls[0].system
    check = next(tl for tl in w.llm.calls[0].tools if tl.name == "check_availability")
    assert "never assume 1" in str(check.parameters)


def test_discount_before_payment_makes_a_new_total_a_new_link_and_closes_the_old_one(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    old_link, old_total, old_advance = b.payment_link_id, b.total, b.advance
    assert w.bot.reissue_link(b, discount=1000, by="owner@x.com") is None
    b = w.repo.get_booking(b.id)
    assert b.total == old_total - 1000 and b.advance == round(b.total * 50 / 100) and b.advance != old_advance
    assert b.payment_link_id != old_link and old_link in w.pay.cancelled
    assert w.pay.created[-1]["amount"] == b.advance and "Discount Rs 1000" in b.notes
    text = w.wa.texts()[-1]
    assert f"₹{b.total:,}" in text and b.payment_link_url in text and "special price" in text
    assert w.bot.reissue_link(b, discount=b.total) and w.bot.reissue_link(b, discount=0) is None  # too big refused; plain resend works


def test_no_discount_after_payment(world):
    w = world
    hold(w, rooms=1)
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.bot.mark_paid(booking_id=b.id)
    assert "waiting for payment" in w.bot.reissue_link(w.repo.get_booking(b.id), discount=500)


def _old_guest(w, *, status="paid", check_in="2026-10-10", check_out="2026-10-12"):
    """A guest who booked long ago: the old payment link is still in the chat log."""
    from datetime import date

    from app.resort.models import Booking, ChatMessage as Chat
    old = datetime(2026, 10, 1, 9, 0, tzinfo=IST)
    b = Booking(w.p1.id, w.deluxe.id, GUEST, date.fromisoformat(check_in), date.fromisoformat(check_out), 2, 0, 1,
                8000, 4000, old, guest_name="Asha Menon", language="en")
    b.hold_expires_at = old
    w.repo.create_hold(b, old)
    b.status = status
    w.repo.save_booking(b)
    guest = w.repo.touch_guest(GUEST, old)
    guest.name = "Asha Menon"
    w.repo.upsert_guest(guest)
    w.repo.add_chat(Chat(GUEST, "in", "room venam 10 Oct", old, "text"))
    w.repo.add_chat(Chat(GUEST, "out", "Pay here to confirm: https://rzp.io/old-link-123", old, "text"))
    return b


def test_an_old_payment_link_never_reaches_the_ai_in_a_new_conversation(world):
    w = world
    _old_guest(w)
    say(w, "I want another room for 20 Nov", GUEST, reply("Sure, which place and how many nights?"))
    call = w.llm.calls[-1]
    seen = " ".join(m.content for m in call.messages if isinstance(m.content, str))
    assert "old-link-123" not in seen and "room venam" not in seen  # the 6 h+ gap hides the old chat
    assert "GUEST RECORDS" in call.system and "2026-10-10 to 2026-10-12" in call.system
    assert "confirmed" in call.system and "Asha Menon" in call.system
    assert "NEW conversation" in call.system


def test_links_inside_the_current_conversation_are_masked_for_the_ai(world):
    w = world
    from app.resort.models import ChatMessage as Chat
    now = w.clock.now()
    w.repo.add_chat(Chat(GUEST, "in", "room venam", now, "text"))
    w.repo.add_chat(Chat(GUEST, "out", "Pay here: https://rzp.io/same-day-link", now, "text"))
    say(w, "what is the status", GUEST, reply("Let me check."))
    seen = " ".join(m.content for m in w.llm.calls[-1].messages if isinstance(m.content, str))
    assert "same-day-link" not in seen and "[link]" in seen


def test_the_reply_language_is_decided_from_the_latest_message_only(world):
    w = world
    say(w, "Alleppey il beach undo", GUEST, reply("Und sir."))
    assert "REPLY LANGUAGE" in w.llm.calls[-1].system and "Manglish" in w.llm.calls[-1].system.split("REPLY LANGUAGE (")[1]
    say(w, "Do you have a room for 3 people on 20 Nov for 2 nights?", GUEST, reply("Yes, we do."))
    tail = w.llm.calls[-1].system.split("REPLY LANGUAGE (")[1]
    assert "English" in tail and "Manglish" not in tail
    say(w, "ഒരു റൂം വേണം", GUEST, reply("Sheri."))
    assert "Manglish" in w.llm.calls[-1].system.split("REPLY LANGUAGE (")[1]
    say(w, "ok", GUEST, reply("[no reply]"))  # a neutral message keeps the guest's own language so far


def test_the_ai_can_read_the_old_chat_on_demand_with_links_hidden(world):
    w = world
    _old_guest(w)
    say(w, "as I told you earlier, same room as before", GUEST, tool("recall_old_chat"), reply("Yes, I remember."))
    result = last_tool(w)
    text = " ".join(result["messages"])
    assert "room venam 10 Oct" in text and "[link]" in text and "old-link-123" not in text
    assert any(t_["name"] == "recall_old_chat" for t_ in [{"name": x.name} for x in w.llm.calls[-1].tools])


def test_my_bookings_answers_about_an_old_paid_booking(world):
    w = world
    _old_guest(w)
    say(w, "my last booking details?", GUEST, tool("my_bookings"), reply("Your stay was 10 to 12 Oct."))
    rows = last_tool(w)["bookings"]
    assert rows and rows[0]["check_in"] == "2026-10-10" and "payment_link" not in rows[0]


def test_a_short_festival_price_beats_a_long_season_it_falls_in():
    from app.resort.availability import night_rate
    rt = RoomType("P", "Room", base_rate=2500)
    season = RateRule("RT", date(2026, 12, 15), date(2027, 1, 5), 3500, "Christmas")
    festival = RateRule("RT", date(2026, 12, 20), date(2026, 12, 21), 4999, "Festival")
    for rules in ([season, festival], [festival, season]):
        assert night_rate(rt, rules, date(2026, 12, 20)) == 4999
        assert night_rate(rt, rules, date(2026, 12, 24)) == 3500
        assert night_rate(rt, rules, date(2026, 12, 10)) == 2500
