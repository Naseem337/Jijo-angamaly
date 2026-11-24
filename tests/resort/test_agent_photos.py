"""Step AI-2b: the owner's photo flow in the agent (send_photos, the order after a room check, the WhatsApp ids) and the
audit of the old helpers the agent reuses: they must not read the guest's words, the old chat log or the old bot's state.
No LLM: the handlers are called directly."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from test_bot import GUEST, STAFF, hold, world  # noqa: F401  (world is a fixture)

from app.resort import photos as ph
from app.resort.agent.outbox import Outbox, bot_outbox
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import AgentTools
from app.resort.db import SoldOut
from app.resort.i18n import t
from app.resort.models import Booking, ChatMessage, RoomType
from app.resort.payments import PaymentError

STAY = {"place": "Alleppey", "check_in": "2026-11-10", "check_out": "2026-11-12", "adults": 2}
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 50


@pytest.fixture()
def pw(world, tmp_path, monkeypatch):
    """The test resort with a glimpse, 3 general photos and 2 photos for each room type (real files, so they can be sent)."""
    monkeypatch.setenv("RESORT_DB_PATH", str(tmp_path / "r.sqlite3"))
    monkeypatch.setattr(ph, "_shrink", lambda data: None)
    w = world
    w.general = [ph.save_photo(PNG + b"general" + bytes([i])) for i in range(3)]
    w.p1.intro, w.p1.photo_urls = "Beachfront from {price} a night in the {room}.", w.general
    w.repo.save_property(w.p1)
    w.refs = {}
    for name in ("deluxe", "family"):
        rt = w.repo.get_room_type(getattr(w, name).id)
        w.refs[name] = [ph.save_photo(PNG + name.encode() + bytes([i])) for i in range(2)]
        w.repo.save_room_type(RoomType(**{**rt.__dict__, "photo_urls": w.refs[name]}))
    w.wa.sent.clear()
    return w


def run(w, call, args=None, *, live=True):
    """One tool call. live: the outbox really sends through the bot (photos are logged in the chat with their WhatsApp
    id, so 'never twice' works across calls); otherwise a dry run."""
    box = bot_outbox(w.bot, GUEST, dry_run=not live)
    tools = AgentTools(w.bot, GUEST, box)
    result = getattr(tools, call)(args or {})
    box.flush()
    return result, [(i.key, i.text, list(i.refs)) for i in box.sent if i.kind != "marker"], box


def keys(items):
    return [k for k, _, _ in items]


def photo_rows(w):
    return [m.text[len("[photo] "):] for m in w.repo.list_chat(GUEST, limit=300) if m.text.startswith("[photo] ")]


def block(w, rt, rooms, check_in=date(2026, 11, 10), check_out=date(2026, 11, 12)):
    b = Booking(w.p1.id, rt.id, "+919811111111", check_in, check_out, 2 * rooms, 0, rooms, 8000, 4000, w.clock.now(),
                hold_expires_at=w.clock.now() + timedelta(minutes=15))
    w.repo.create_hold(b, w.clock.now())


# --------------------------------------------------------------------------- the order after a room check (rule 2)


def test_after_a_room_check_the_owners_order_goes(pw):
    w = pw
    result, items, _ = run(w, "update_enquiry", STAY)
    assert result["sent"] == ["rooms_ok", "rooms_plain", "glimpse", "photos:3", "room_label", "photos:2", "ask_contact"]
    by = {k: (text, refs) for k, text, refs in items}
    assert by["glimpse"][0] == "Beachfront from ₹4,000 a night in the Deluxe."
    assert by["room_label"][0] == "*Deluxe*"
    photos = [refs for k, _, refs in items if k.startswith("photos:")]
    assert photos == [w.general, w.refs["deluxe"]]  # general first, then the offered room's own photos
    assert photo_rows(w) == w.general + w.refs["deluxe"]  # really sent, and logged with their WhatsApp ids


def test_the_glimpse_goes_once_and_a_second_check_sends_no_photo_twice(pw):
    w = pw
    run(w, "update_enquiry", STAY)
    result, items, _ = run(w, "update_enquiry", {**STAY, "check_in": "2026-11-20", "check_out": "2026-11-22"})  # new dates
    assert result["sent"] == ["rooms_ok", "rooms_plain", "ask_contact"]  # glimpse once, every photo already sent
    assert photo_rows(w) == w.general + w.refs["deluxe"]


def test_a_new_room_after_a_change_of_persons_gets_only_its_own_photos(pw):
    w = pw
    run(w, "update_enquiry", STAY)
    result, items, _ = run(w, "update_enquiry", {**STAY, "adults": 4, "rooms": 1})  # now the Family Suite fits one room
    assert result["sent"] == ["rooms_plain", "room_label", "photos:2", "ask_contact"]
    assert items[1][1] == "*Family Suite*" and photo_rows(w)[-2:] == w.refs["family"]


def test_a_group_split_over_two_room_types_shows_both(pw):
    w = pw
    result, items, _ = run(w, "update_enquiry", {**STAY, "room_picks": [{"room_type": "Deluxe", "count": 1},
                                                                         {"room_type": "Family Suite", "count": 1}]})
    assert [text for k, text, _ in items if k == "room_label"] == ["*Deluxe*", "*Family Suite*"]


def test_room_numbers_label_each_rooms_photos(pw):
    w = pw
    rt = w.repo.get_room_type(w.deluxe.id)
    own = {"108": [ph.save_photo(PNG + b"r108")], "109": [ph.save_photo(PNG + b"r109")]}
    w.repo.save_room_type(RoomType(**{**rt.__dict__, "room_list": ["108", "109"], "number_photos": own, "photo_urls": []}))
    _, items, _ = run(w, "update_enquiry", STAY, live=False)
    assert [text for k, text, _ in items if k == "room_label"] == ["*Room 108 - Deluxe*"]  # the room the guest would get


# --------------------------------------------------------------------------- one test for each kind


def checked(w):
    run(w, "update_enquiry", STAY)
    w.wa.sent.clear()


def test_kind_default_gives_the_offered_room_then_the_next_free_one_then_photos_sent(pw):
    w = pw
    run(w, "update_enquiry", STAY)  # the owner's order sent the general photos and the Deluxe photos
    w.wa.sent.clear()
    one, items, _ = run(w, "send_photos", {"kind": "default"})
    assert keys(items) == ["room_label", "photos:2", "ask_contact"] and items[0][1] == "*Family Suite*"  # Deluxe already went
    two, items, _ = run(w, "send_photos", {"kind": "default"})
    assert keys(items) == ["photos_sent"] and items[0][1] == "All the photos are sent above."


def test_kind_more_goes_on_with_the_next_photos(pw):
    w = pw
    run(w, "update_enquiry", STAY)
    result, items, _ = run(w, "send_photos", {"kind": "more"})
    assert result["facts"]["rooms"] == ["Family Suite"] and photo_rows(w)[-2:] == w.refs["family"]


def test_kind_room_type_sends_that_rooms_photos_when_it_is_free(pw):
    w = pw
    checked(w)
    result, items, _ = run(w, "send_photos", {"kind": "room_type", "room_type": "family"})
    assert result["ok"] and keys(items)[:3] == ["room_label", "photos:2", "ask_contact"]
    assert items[0][1] == "*Family Suite*" and items[1][2] == w.refs["family"]
    again, items, _ = run(w, "send_photos", {"kind": "room_type", "room_type": "family"})
    assert keys(items) == ["photos_sent"]  # photos already sent


def test_kind_resort_sends_only_the_general_photos_with_no_label(pw):
    w = pw
    run(w, "update_enquiry", {"place": "Alleppey"})  # resort known, no dates: the general photos were not sent yet
    w.wa.sent.clear()
    result, items, _ = run(w, "send_photos", {"kind": "resort"})
    assert [k for k in keys(items) if k != "dates_for_availability"][:1] == ["photos:3"] and items[0][2] == w.general
    assert "room_label" not in keys(items)
    again, items, _ = run(w, "send_photos", {"kind": "resort"})
    assert keys(items) == ["photos_sent"]


def test_kind_other_room_says_it_is_free_then_sends_its_photos(pw):
    w = pw
    checked(w)
    result, items, _ = run(w, "send_photos", {"kind": "other_room"})
    assert keys(items)[:4] == ["other_room_free", "room_label", "photos:2", "ask_contact"]
    assert items[0][1] == "Yes, Family Suite is also free for your dates. Photos below; tell me if you want this room."
    assert photo_rows(w)[-2:] == w.refs["family"]


def test_kind_other_room_when_its_photos_already_went_says_it_is_free(pw):
    w = pw
    checked(w)
    run(w, "send_photos", {"kind": "other_room"})
    result, items, _ = run(w, "send_photos", {"kind": "other_room"})
    assert keys(items) == ["other_free"]
    assert items[0][1] == "Yes, Family Suite is also free for your dates. Tell me if you want this room."


def test_kind_all_rooms_sends_every_free_room_not_shown_yet_one_after_another(pw):
    w = pw
    run(w, "update_enquiry", {"place": "Alleppey"})
    w.wa.sent.clear()
    result, items, _ = run(w, "send_photos", {"kind": "all_rooms"})
    labels = [text for k, text, _ in items if k == "room_label"]
    assert labels == ["*Deluxe*", "*Family Suite*"] and photo_rows(w) == w.refs["deluxe"] + w.refs["family"]
    again, items, _ = run(w, "send_photos", {"kind": "all_rooms"})  # every room shown, before dates: the resort's photos
    assert keys(items)[0] == "photos:3" and items[0][2] == w.general


# --------------------------------------------------------------------------- when there is nothing to send


def test_photos_already_sent_say_so(pw):
    w = pw
    checked(w)
    run(w, "send_photos", {"kind": "all_rooms"})
    result, items, _ = run(w, "send_photos", {"kind": "default"})
    assert result["ok"] and keys(items) == ["photos_sent"] and items[0][1] == t("photos_sent", "en")


def test_another_room_that_is_not_free_says_no_other_room(pw):
    w = pw
    block(w, w.family, 3)
    checked(w)
    result, items, _ = run(w, "send_photos", {"kind": "other_room"})
    assert keys(items) == ["no_other_room"] and items[0][1] == "Sorry, no other room is free on your dates."


def test_a_named_room_that_is_not_free_says_room_not_free(pw):
    w = pw
    block(w, w.family, 3)
    checked(w)
    result, items, _ = run(w, "send_photos", {"kind": "room_type", "room_type": "Family Suite"})
    assert keys(items) == ["room_not_free"] and items[0][1] == "Sorry, Family Suite is not free on your dates."
    assert photo_rows(w) == w.general + w.refs["deluxe"]  # nothing more went


def test_a_room_that_does_not_exist_is_a_problem_for_the_ai_not_a_message(pw):
    run(pw, "update_enquiry", {"place": "Alleppey"})  # the resort is known
    result, items, _ = run(pw, "send_photos", {"kind": "room_type", "room_type": "Penthouse"})
    assert not result["ok"] and "no room type 'Penthouse'" in result["facts"]["problem"] and items == []
    bad, items, _ = run(pw, "send_photos", {"kind": "selfie"})
    assert not bad["ok"] and items == []


def test_no_photo_uploaded_at_all_sends_nothing(world):
    w = world  # the plain test world has no photos
    run(w, "update_enquiry", {"place": "Alleppey"})
    result, items, _ = run(w, "send_photos", {"kind": "default"}, live=False)
    assert result["ok"] and result["sent"] == [] and "no photo has been uploaded" in result["facts"]["info"]


# --------------------------------------------------------------------------- no resort yet, no dates yet (rules 5 and 6)


def test_photos_asked_before_any_resort_ask_the_location_first_and_keep_the_request(pw):
    w = pw
    result, items, _ = run(w, "send_photos", {"kind": "room_type", "room_type": "Family Suite"})
    assert keys(items) == ["ask_location"] and items[0][1] == "Sure, Kindly confirm the Location 📍\nAlleppey or Munnar ?"
    assert result["facts"]["waiting_for"] == "location" and result["next_step"] == "ask_location"
    assert Enquiry.load(w.repo, GUEST).photo_wish == {"kind": "room_type", "room_type": "Family Suite"}
    assert photo_rows(w) == []
    # the guest answers: the photos go right after, then the dates line and the form (once)
    answer, items, _ = run(w, "update_enquiry", {"place": "Alleppey"})
    assert keys(items) == ["room_label", "photos:2", "dates_for_availability", "form"]
    assert items[0][1] == "*Family Suite*" and photo_rows(w) == w.refs["family"]
    assert items[2][1] == "To check which rooms are available, we need your check-in and check-out dates."
    assert items[3][1].startswith("Please share these details")
    assert Enquiry.load(w.repo, GUEST).photo_wish is None  # asked once, answered once


def test_photos_before_the_dates_send_the_dates_line_and_the_form_once(pw):
    w = pw
    Enquiry(place=w.p1.id).save(w.repo, GUEST)  # the resort is known, no form has gone yet
    result, items, _ = run(w, "send_photos", {"kind": "default"})
    assert keys(items) == ["room_label", "photos:2", "dates_for_availability", "form"]
    assert items[2][1] == t("dates_for_availability", "en") and items[3][1].startswith("Please share these details")
    more, items, _ = run(w, "send_photos", {"kind": "more"})
    # no second dates line; the open step is repeated after the photos (rule 8), as the old bot does
    assert keys(items) == ["room_label", "photos:2", "form"]


def test_photos_with_a_payment_link_open_are_not_followed_by_a_booking_step(pw):
    w = pw
    hold(w)
    w.wa.sent.clear()
    result, items, _ = run(w, "send_photos", {"kind": "room_type", "room_type": "Family Suite"})
    assert result["ok"] and keys(items) == ["room_label", "photos:2"] and result["next_step"] == "pay"


# --------------------------------------------------------------------------- the WhatsApp ids (rule 8)


def test_each_photo_and_label_keeps_its_whatsapp_id_so_a_swipe_reply_finds_its_room(pw):
    w = pw
    run(w, "update_enquiry", STAY)
    rows = w.repo.list_chat(GUEST, limit=300)
    photos = [m for m in rows if m.direction == "out" and m.text.startswith("[photo] ")]
    labels = [m for m in rows if m.direction == "out" and m.text == "*Deluxe*"]
    assert len(photos) == 5 and all(m.wamid for m in photos) and labels and labels[0].wamid
    assert len({m.wamid for m in photos}) == 5  # every photo has its own id
    deluxe_photo = next(m for m in photos if m.text == f"[photo] {w.refs['deluxe'][0]}")
    w.bot._note_quote(GUEST, deluxe_photo.wamid)  # the guest swipes on that photo
    assert w.bot._quoted_rooms[GUEST] == w.deluxe.id
    assert w.bot._room_of_message(labels[0].text)[0].id == w.deluxe.id  # and on its label


def test_a_dry_run_sends_nothing_and_logs_nothing(pw):
    w = pw
    result, items, _ = run(w, "update_enquiry", STAY, live=False)
    assert result["sent"][2:5] == ["glimpse", "photos:3", "room_label"] and w.wa.sent == [] and photo_rows(w) == []


# --------------------------------------------------------------------------- audit: the reused helpers read nothing old


def test_the_change_after_a_link_ignores_the_old_bots_state_and_chat(pw):
    w = pw
    hold(w)  # Asha Menon, Deluxe x 1, 10-12 Nov, 2 persons
    state = w.repo.get_state(GUEST)  # the old bot's state says something else entirely
    state.update({"bk": {"property_id": w.p2.id, "check_in": "2027-01-01", "check_out": "2027-01-05", "adults": 9},
                  "checked": "x|y|z", "gs": "ml", "lang": "ml", "style": "manglish", "room_pref": w.family.id,
                  "room_mix": [w.family.id, w.deluxe.id]})
    w.repo.set_state(GUEST, state)
    w.repo.add_chat(ChatMessage(GUEST, "in", "My name is Ravi 9999999999, I want 7 rooms in Munnar", w.clock.now(), "text"))
    old = w.repo.list_bookings(phone=GUEST)[0]
    tools = AgentTools(w.bot, GUEST, Outbox(GUEST, dry_run=True))
    result = tools.update_enquiry({**STAY, "adults": 4})
    texts = {i.key: i.text for i in tools.out.queue}
    assert result["ok"] and result["sent"][-2:] == ["summary", "pay"]
    assert "Asha Menon" in texts["summary"] and "Ravi" not in texts["summary"]  # the held name, not the chat's
    assert "Check in - 10th Nov 26" in texts["summary"] and "27" not in texts["summary"].split("Check out")[0]  # the Enquiry's dates
    assert texts["pay"].startswith("Please pay the advance using this link:")  # English: the Enquiry's language, not 'ml'
    live = [b for b in w.repo.list_bookings(phone=GUEST) if b.status == "hold"]
    assert len(live) == 1 and live[0].rooms == 2 and live[0].property_id == w.p1.id and live[0].id != old.id


def test_a_failed_payment_link_in_a_change_goes_through_the_outbox_not_straight_to_the_guest(pw):
    w = pw
    hold(w)
    w.wa.sent.clear()

    def boom(*a, **k):
        raise PaymentError("down")

    w.pay.create_payment_link = boom
    tools = AgentTools(w.bot, GUEST, Outbox(GUEST, dry_run=True))
    result = tools.update_enquiry({**STAY, "adults": 4})
    texts = {i.key: i.text for i in tools.out.queue}
    assert not result["ok"] and "pay_failed_link" in texts and "9497861924" in texts["pay_failed_link"]
    assert w.wa.to(GUEST) == []  # nothing went to the guest past the outbox (a dry run sends nothing)
    assert w.wa.to(STAFF)  # the owner's team was alerted


def test_a_sold_out_race_in_a_change_tells_the_guest_through_the_outbox(pw):
    w = pw
    hold(w)
    w.wa.sent.clear()

    def taken(*a, **k):
        raise SoldOut("gone")

    w.repo.create_hold = taken
    tools = AgentTools(w.bot, GUEST, Outbox(GUEST, dry_run=True))
    result = tools.update_enquiry({**STAY, "adults": 4})
    texts = {i.key: i.text for i in tools.out.queue}
    assert not result["ok"] and texts.get("rooms_none") == "Sorry, rooms are not available for your dates."
    assert w.wa.to(GUEST) == []


def test_the_room_check_helpers_depend_only_on_what_they_are_given(pw):
    """_fits, _mix and _no_room_answer: the same answer whatever the old bot's state or chat say."""
    w = pw
    prop = w.repo.get_property(w.p1.id)
    bk = {"property_id": prop.id, "check_in": "2026-11-10", "check_out": "2026-11-12", "adults": 4, "children": 0,
          "rooms": 0, "own": GUEST}
    before = ([(rt.id, o) for rt, o in w.bot._fits(prop, bk)], w.bot._mix(prop, bk, 0))
    w.repo.set_state(GUEST, {"bk": {"adults": 99}, "checked": "none|x", "room_pref": w.family.id, "lang": "ml"})
    w.repo.add_chat(ChatMessage(GUEST, "in", "I want all the family suites", w.clock.now(), "text"))
    after = ([(rt.id, o) for rt, o in w.bot._fits(prop, bk)], w.bot._mix(prop, bk, 0))
    assert before == after
    block(w, w.deluxe, 2)
    block(w, w.family, 3)
    scratch = {"lang": "en"}
    text = w.bot._no_room_answer(scratch, prop, bk, date(2026, 11, 10), date(2026, 11, 12), 4, 0, 0)
    assert w.repo.get_state(GUEST)["bk"] == {"adults": 99}  # the old state was not touched
    assert text and set(scratch) <= {"lang", "alt_dates", "suggest_rooms"}  # only its own scratch hints were written
