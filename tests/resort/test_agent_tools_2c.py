"""Step AI-2c: the remaining tools (create_booking, check_payment, send_payment_link, send_map, my_bookings, change_name,
talk_to_owner, recall_old_chat), the 11 schemas, and the three fixes before it: staff alerts through the outbox, a dry run
that never calls WhatsApp, and a name that only comes from the tool argument. No LLM: handlers are called directly."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from test_agent_photos import pw  # noqa: F401  (fixture: the test resort with a glimpse and photos)
from test_bot import GUEST, STAFF, hold, world  # noqa: F401  (world is a fixture)

from app.resort.agent import tools as tools_module
from app.resort.agent.outbox import Outbox, bot_outbox
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import TOOL_SCHEMAS, AgentTools
from app.resort.db import SoldOut
from app.resort.models import ChatMessage
from app.resort.payments import PaymentError

STAY = {"place": "Alleppey", "check_in": "2026-11-10", "check_out": "2026-11-12", "adults": 2}
SHAPE = {"ok", "facts", "sent", "next_step", "note"}


class Spy:
    """Stands in for the WhatsApp client and records every call made on it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            return "wamid.spy"
        return call


def run(w, name, args=None, *, live=False):
    box = bot_outbox(w.bot, GUEST, dry_run=not live)
    result = AgentTools(w.bot, GUEST, box).call(name, args or {})
    box.flush()
    return result, {i.key: i.text for i in box.sent}, box


def digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def book(w, name="Asha", mobile="same", live=False):
    run(w, "update_enquiry", STAY, live=live)
    return run(w, "create_booking", {"guest_name": name, "mobile": mobile}, live=live)


def all_booking_ids(w):
    return [b.id for b in w.repo.list_bookings(phone=GUEST, limit=50)]


# --------------------------------------------------------------------------- fix 1: staff alerts through the outbox


def test_a_staff_alert_is_only_recorded_in_a_dry_run_and_sent_when_live(world):
    w = world
    result, texts, box = run(w, "talk_to_owner", {"reason": "wants a refund"})
    staff = [i for i in box.sent if i.kind == "staff"]
    assert staff and STAFF in staff[0].to and staff[0].text.startswith("Guest needs the team: wants a refund")
    assert w.wa.to(STAFF) == [] and w.wa.to(GUEST) == []  # dry run: nothing sent
    run(w, "talk_to_owner", {"reason": "wants a refund"}, live=True)
    assert [m for m in w.wa.texts(STAFF) if m.startswith("Guest needs the team: wants a refund")]  # live: the team hears
    assert w.wa.texts(GUEST)[-1].startswith("Thank you. Please contact the team directly:")


def test_guest_called_sends_the_owners_thank_you_and_alerts_no_one(world):
    w = world
    result, texts, box = run(w, "talk_to_owner", {"reason": "guest_called"})
    assert result["ok"] and result["sent"] == ["guest_called"]
    assert texts["guest_called"] == "Thank you sir. Please let me know if you need any help with your booking."
    assert [i for i in box.sent if i.kind == "staff"] == []
    run(w, "talk_to_owner", {"reason": "guest_called"}, live=True)
    assert w.wa.to(STAFF) == []  # live: still no staff alert
    assert w.wa.texts(GUEST)[-1] == "Thank you sir. Please let me know if you need any help with your booking."


def test_pay_terms_sends_the_owners_exact_text_with_the_numbers_and_tells_the_team(world):
    w = world
    run(w, "update_enquiry", STAY)
    result, texts, box = run(w, "talk_to_owner", {"reason": "pay_terms"})
    assert result["ok"] and result["sent"] == ["pay_terms", "staff:alert"]
    assert texts["pay_terms"] == ("For More detailed discussion, kindly call to the below numbers: "
                                  "9497861924 / 9995855789")
    assert "other payment terms" in [i for i in box.sent if i.kind == "staff"][0].text
    assert "pay_terms" in (t := next(x for x in TOOL_SCHEMAS if x.name == "talk_to_owner")).description and t


def test_guest_called_with_an_open_step_sends_the_step_after_the_thank_you(world):
    w = world
    run(w, "update_enquiry", STAY)
    result, texts, box = run(w, "talk_to_owner", {"reason": "guest_called", "language": "en"})
    assert result["sent"][0] == "guest_called" and len(result["sent"]) == 2  # the thank-you, then the open step
    assert [i for i in box.sent if i.kind == "staff"] == []


def test_a_payment_failure_alerts_the_team_through_the_outbox(world):
    w = world
    run(w, "update_enquiry", STAY)

    def boom(*a, **k):
        raise PaymentError("down")

    w.pay.create_payment_link = boom
    result, texts, box = run(w, "create_booking", {"guest_name": "Asha", "mobile": "same"})
    assert not result["ok"] and result["sent"] == ["pay_failed_link", "staff:alert"]
    assert "9497861924" in digits(texts["pay_failed_link"]) and w.wa.sent == []
    live, _, _ = run(w, "create_booking", {"guest_name": "Asha", "mobile": "same"}, live=True)
    assert any(m.startswith("Guest needs the team: payment link could not be created") for m in w.wa.texts(STAFF))


def test_a_paid_booking_alerts_the_team_through_the_outbox_when_live(world):
    w = world
    book(w, live=True)
    w.wa.sent.clear()
    link = w.repo.list_bookings(phone=GUEST)[0].payment_link_id
    w.pay.paid_links[link] = "pay_1"
    w.bot._pay_checked = {}
    result, texts, box = run(w, "check_payment", live=True)
    assert result["facts"]["paid"] is True and "staff:alert" in result["sent"]
    assert any("PAID" in m for m in w.wa.texts(STAFF))


# --------------------------------------------------------------------------- fix 2: a dry run never calls WhatsApp


def test_a_full_dry_run_turn_with_photos_makes_zero_whatsapp_calls(pw):
    w = pw
    spy = Spy()
    w.bot.wa = spy
    rows_before = len(w.repo.list_chat(GUEST, limit=500))
    turn = [("update_enquiry", STAY), ("send_photos", {"kind": "resort"}), ("send_photos", {"kind": "all_rooms"}),
            ("send_photos", {"kind": "room_type", "room_type": "Family Suite"}), ("get_price", {}), ("send_map", {}),
            ("create_booking", {"guest_name": "Asha", "mobile": "same"}), ("send_payment_link", {"reason": "hold"}),
            ("check_payment", {}), ("my_bookings", {}), ("change_name", {"name": "Asha M"}),
            ("talk_to_owner", {"reason": "asks a discount"}), ("recall_old_chat", {}),
            ("update_enquiry", {**STAY, "adults": 4})]  # the last one changes the stay after the link
    photos = 0
    for name, args in turn:
        result, texts, box = run(w, name, args)
        assert set(result) == SHAPE
        photos += sum(1 for i in box.sent if i.kind == "photos")
    assert photos >= 3  # photos really were part of the turn
    assert spy.calls == []  # not one WhatsApp call: no text, no upload, no photo, no label, no staff alert
    assert not [m for m in w.repo.list_chat(GUEST, limit=500) if m.text.startswith("[photo]")]  # no WhatsApp ids tracked
    assert all(m.direction == "in" or m.wamid == "" for m in w.repo.list_chat(GUEST, limit=500)[rows_before:])


def test_a_dry_run_outbox_has_no_senders_called_even_when_given(world):
    called = []
    box = Outbox(GUEST, lambda *a: called.append("t"), lambda *a: called.append("p"), dry_run=True,
                 send_staff=lambda *a: called.append("s"))
    box.text("x", "hello")
    box.photos("photos:1", ["a"])
    box.staff("staff:alert", "alert", [STAFF])
    assert [i.kind for i in box.flush()] == ["text", "photos", "staff"] and called == []


# --------------------------------------------------------------------------- fix 3: the name only comes from the argument


def test_the_name_never_comes_from_the_chat_and_empty_is_allowed(world, monkeypatch):
    w = world
    w.repo.add_chat(ChatMessage(GUEST, "in", "Ravi 9999999999 here", w.clock.now(), "text"))

    def never(*a, **k):
        raise AssertionError("_name_in_chat must not run in the agent path")

    monkeypatch.setattr(w.bot, "_name_in_chat", never)
    run(w, "update_enquiry", STAY)
    result, texts, _ = run(w, "create_booking", {"guest_name": "", "mobile": "same"})
    assert result["ok"] and "Guest -" not in texts["summary"] and "Ravi" not in texts["summary"]
    assert w.repo.list_bookings(phone=GUEST)[0].guest_name == ""
    named, texts, _ = book(w, name="Asha", mobile="same")
    assert named["ok"] and "Guest - Asha" in texts["summary"]


def test_sold_out_in_the_agent_path_does_not_read_the_chat_log(world, monkeypatch):
    w = world
    run(w, "update_enquiry", STAY)

    def taken(*a, **k):
        raise SoldOut("gone")

    def never(*a, **k):
        raise AssertionError("the chat log must not be read in the agent path")

    monkeypatch.setattr(w.repo, "create_hold", taken)
    monkeypatch.setattr(w.repo, "last_outbound_text", never)
    result, texts, _ = run(w, "create_booking", {"guest_name": "Asha", "mobile": "same"})
    assert not result["ok"] and texts["rooms_none"] == "Sorry, rooms are not available for your dates."


# --------------------------------------------------------------------------- create_booking


def test_create_booking_with_same_mobile_sends_the_summary_and_the_link(world):
    w = world
    result, texts, _ = book(w, mobile="same")
    assert set(result) == SHAPE and result["ok"] and result["sent"] == ["summary", "pay"]
    assert "Guest - Asha" in texts["summary"] and "98470 12345" in texts["summary"]  # 'same' = the WhatsApp number
    assert texts["pay"].startswith("Please pay the advance using this link:\n")
    b = w.repo.list_bookings(phone=GUEST)[0]
    assert b.status == "hold" and b.payment_link_url in texts["pay"] and "Mobile: 9847012345" in b.notes
    enq = Enquiry.load(w.repo, GUEST)
    assert enq.booking_id == b.id and enq.link_open and result["next_step"] == "pay"
    assert result["facts"] == {"booked": True, "hold_minutes": 15} and b.id not in json.dumps(result)


def test_create_booking_with_the_guests_own_number(world):
    result, texts, _ = book(world, mobile="9876543210")
    assert result["ok"] and "98765 43210" in texts["summary"]


def test_create_booking_without_a_valid_mobile_creates_nothing(world):
    w = world
    run(w, "update_enquiry", STAY)
    bad, texts, _ = run(w, "create_booking", {"guest_name": "Asha", "mobile": "12"})
    assert not bad["ok"] and "does not look right" in bad["facts"]["problem"] and texts == {}
    none, texts, _ = run(w, "create_booking", {"guest_name": "Asha", "mobile": ""})
    assert not none["ok"] and "mobile number" in none["facts"]["problem"] and texts == {}
    assert w.repo.list_bookings(phone=GUEST) == []


def test_create_booking_before_any_room_check_is_a_problem(world):
    result, texts, _ = run(world, "create_booking", {"guest_name": "Asha", "mobile": "same"})
    assert not result["ok"] and texts == {} and "update_enquiry" in result["note"]


def test_create_booking_for_a_group_over_two_room_types_makes_one_link(world):
    w = world
    run(w, "update_enquiry", {**STAY, "room_picks": [{"room_type": "Deluxe", "count": 1},
                                                      {"room_type": "Family Suite", "count": 1}]})
    result, texts, _ = run(w, "create_booking", {"guest_name": "Asha", "mobile": "same"})
    assert result["ok"] and result["sent"] == ["summary", "pay"]
    assert len([b for b in w.repo.list_bookings(phone=GUEST) if b.status == "hold"]) == 2 and len(w.pay.created) == 1


# --------------------------------------------------------------------------- check_payment


def test_check_payment_with_no_open_link_says_what_is_missing(world):
    w = world
    result, texts, _ = run(w, "check_payment")
    assert result["ok"] and texts == {} and result["facts"]["reason"] == "no open payment link"
    assert result["next_step"] == "ask_location"


def test_check_payment_not_paid_sends_pay_wait(world):
    w = world
    book(w)
    w.bot._pay_checked = {}
    result, texts, _ = run(w, "check_payment")
    assert result["facts"] == {"paid": False} and texts["pay_wait"].startswith("We have not received the payment yet.")
    assert "confirmed" not in result["sent"] and w.repo.list_bookings(phone=GUEST)[0].status == "hold"


def test_check_payment_is_throttled_to_once_every_15_seconds(world, monkeypatch):
    w = world
    book(w)
    w.bot._pay_checked = {}
    asked = []
    real = w.pay.link_status
    monkeypatch.setattr(w.pay, "link_status", lambda link_id: asked.append(link_id) or real(link_id))
    first, _, _ = run(w, "check_payment")
    second, texts, _ = run(w, "check_payment")
    assert first["facts"] == {"paid": False} and second["facts"] == {"paid": False, "throttled": True}
    assert len(asked) == 1 and "pay_wait" in texts  # the provider was asked once; the guest still gets an answer
    w.bot._pay_checked[GUEST] -= 16  # 16 seconds later
    run(w, "check_payment")
    assert len(asked) == 2


def test_check_payment_only_asks_about_the_guests_own_link(world, monkeypatch):
    w = world
    book(w)
    other = "+919811111111"
    run_other = bot_outbox(w.bot, other, dry_run=True)
    AgentTools(w.bot, other, run_other).update_enquiry({**STAY, "adults": 2})
    AgentTools(w.bot, other, run_other).create_booking({"guest_name": "Ravi", "mobile": "same"})
    asked = []
    real = w.pay.link_status
    monkeypatch.setattr(w.pay, "link_status", lambda link_id: asked.append(link_id) or real(link_id))
    w.bot._pay_checked = {}
    run(w, "check_payment")
    mine = w.repo.list_bookings(phone=GUEST)[0].payment_link_id
    assert asked == [mine]


def test_check_payment_paid_runs_the_confirmation_flow_through_the_outbox(world):
    w = world
    book(w, live=True)
    w.wa.sent.clear()
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.pay.paid_links[b.payment_link_id] = "pay_1"
    w.bot._pay_checked = {}
    result, texts, _ = run(w, "check_payment", live=True)
    assert result["ok"] and result["facts"] == {"paid": True} and result["next_step"] == "done"
    assert result["sent"][:3] == ["confirmed", "policies", "staff:alert"]
    assert "Following are the glimpse of your booking" in texts["confirmed"] and "Booking Confirmed" in texts["confirmed"]
    assert w.repo.get_booking(b.id).status == "paid"
    assert "Booking Confirmed" in w.wa.texts(GUEST)[0] and w.wa.texts(GUEST)[1] == "Free cancellation 7 days before."
    assert Enquiry.load(w.repo, GUEST).paid
    again, texts, _ = run(w, "check_payment", live=True)  # the guest says 'paid' once more
    assert again["facts"] == {"already_paid": True} and texts == {}


def test_check_payment_paid_in_a_dry_run_applies_nothing(world):
    w = world
    book(w, live=True)
    w.wa.sent.clear()
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.pay.paid_links[b.payment_link_id] = "pay_1"
    w.bot._pay_checked = {}
    result, texts, _ = run(w, "check_payment")
    assert result["facts"] == {"paid": True, "applied": False} and w.repo.get_booking(b.id).status == "hold"
    assert w.wa.sent == []


# --------------------------------------------------------------------------- send_payment_link


def test_send_payment_link_lost_sends_the_link_and_hold_sends_the_pay_now_line(world):
    w = world
    book(w)
    url = w.repo.list_bookings(phone=GUEST)[0].payment_link_url
    lost, texts, _ = run(w, "send_payment_link", {"reason": "lost"})
    assert lost["ok"] and lost["sent"] == ["pay"] and texts["pay"] == f"Please pay the advance using this link:\n{url}"
    held, texts, _ = run(w, "send_payment_link", {"reason": "hold"})
    assert held["sent"] == ["hold_pay"] and texts["hold_pay"] == f"To hold the room, please pay the advance now using this link:\n{url}"
    again, texts, _ = run(w, "send_payment_link", {"reason": "not_paid_yet"})
    assert again["sent"] == ["pay"] and again["next_step"] == "pay"


def test_send_payment_link_after_the_hold_ran_out_checks_the_same_stay_and_sends_a_new_link(world):
    w = world  # fix round 2: was "hold expired, nothing sent" (the AI then explained the expiry to the guest)
    book(w)
    w.clock.advance(minutes=20)  # the hold is 15 minutes
    result, texts, _ = run(w, "send_payment_link", {"reason": "lost"}, live=True)
    assert result["ok"] and result["sent"][-2:] == ["summary", "pay"] and "rooms_ok" not in result["sent"]
    assert result["next_step"] == "pay" and "http" in texts["pay"]


def test_send_payment_link_with_no_booking_or_a_bad_reason(world):
    w = world
    none, texts, _ = run(w, "send_payment_link", {"reason": "lost"})
    assert not none["ok"] and none["facts"] == {"open_link": False} and texts == {}
    bad, texts, _ = run(w, "send_payment_link", {"reason": "please"})
    assert not bad["ok"] and texts == {}


# --------------------------------------------------------------------------- send_map


def test_send_map_sends_the_link_then_the_open_step(world):
    w = world
    run(w, "update_enquiry", {"place": "Alleppey"})
    result, texts, _ = run(w, "send_map")
    assert result["ok"] and result["sent"] == ["map", "form"]
    assert texts["map"] == "\U0001F4CD Pozhiyoram Resort\nhttps://maps.app.goo.gl/abc"


def test_send_map_needs_a_resort_and_a_saved_link(world):
    w = world
    unknown, texts, _ = run(w, "send_map")
    assert not unknown["ok"] and texts == {}  # two resorts and none chosen
    run(w, "update_enquiry", {"place": "Munnar"})
    no_link, texts, _ = run(w, "send_map")
    assert no_link["ok"] and no_link["facts"] == {"map": False} and "map" not in texts


# --------------------------------------------------------------------------- my_bookings (read only)


def test_my_bookings_is_read_only_and_never_gives_a_booking_id_or_link(world):
    w = world
    book(w, live=True)
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.pay.paid_links[b.payment_link_id] = "pay_1"
    w.bot._pay_checked = {}
    run(w, "check_payment", live=True)  # now paid
    result, texts, box = run(w, "my_bookings")
    blob = json.dumps(result)
    assert result["ok"] and texts == {} and box.sent == []  # read only
    assert b.id not in blob and "RB-" not in blob and "rzp.io" not in blob and b.payment_link_url not in blob
    row = result["facts"]["bookings"][0]
    assert row["status"] == "confirmed" and row["check_in"] == "2026-11-10" and row["rooms"] == 1
    terms = row["if_cancelled_today"]  # 8 days before check-in: the owner's 10% retention row
    assert terms["retention_pct"] == 10 and terms["retention_amount"] == 800 and terms["refund_of_paid"] == 3200


def test_my_bookings_for_a_booking_on_hold_and_for_a_new_guest(world):
    w = world
    empty, _, _ = run(w, "my_bookings")
    assert empty["facts"] == {"bookings": [], "info": "this guest has no booking yet"}
    book(w)
    row = run(w, "my_bookings")[0]["facts"]["bookings"][0]
    assert row["status"] == "on hold" and row["payment_link_open"] is True and "if_cancelled_today" not in row


# --------------------------------------------------------------------------- change_name


def test_change_name_updates_the_bookings_and_tells_the_guest(world):
    w = world
    book(w, name="Naseem")
    result, texts, _ = run(w, "change_name", {"name": "shibil k"})
    assert result["ok"] and result["facts"] == {"bookings_changed": 1} and texts == {"name_changed": "Done, the name is changed to Shibil K."}
    assert w.repo.list_bookings(phone=GUEST)[0].guest_name == "Shibil K"
    assert w.repo.get_guest(GUEST).name == "Shibil K"
    bad, texts, _ = run(w, "change_name", {"name": "12345"})
    assert not bad["ok"] and texts == {}


# --------------------------------------------------------------------------- talk_to_owner


def test_talk_to_owner_tells_the_guest_the_numbers_and_the_team_the_summary(world):
    w = world
    run(w, "update_enquiry", {"place": "Alleppey"})
    result, texts, box = run(w, "talk_to_owner", {"reason": "wants a discount of 50 percent"})
    assert result["ok"] and result["sent"] == ["handoff", "staff:alert"]
    assert texts["handoff"].startswith("Thank you. Please contact the team directly:")
    assert "9497861924" in digits(texts["handoff"]) and "9995855789" in digits(texts["handoff"])
    assert "will contact" not in texts["handoff"]
    staff = next(i for i in box.sent if i.kind == "staff")
    assert staff.text.startswith("Guest needs the team: wants a discount of 50 percent") and STAFF in staff.to


# --------------------------------------------------------------------------- recall_old_chat (read only)


def test_recall_old_chat_gives_earlier_conversations_with_links_hidden(world):
    w = world
    before = w.clock.now() - timedelta(days=3)
    w.repo.add_chat(ChatMessage(GUEST, "in", "I want a cottage in Alleppey for 5 persons", before, "text"))
    w.repo.add_chat(ChatMessage(GUEST, "out", "Pay here https://rzp.io/abc please", before + timedelta(minutes=1), "text"))
    w.repo.add_chat(ChatMessage(GUEST, "in", "hello again", w.clock.now(), "text"))
    result, texts, box = run(w, "recall_old_chat", {"query": "cottage"})
    assert result["ok"] and texts == {} and box.sent == []
    lines = result["facts"]["messages"]
    assert lines == ["30 Oct Guest: I want a cottage in Alleppey for 5 persons"]  # the matching line; today's chat is not "old"
    allof = run(w, "recall_old_chat", {})[0]["facts"]["messages"]
    assert allof[1] == "30 Oct Bot: Pay here [link] please" and not any("rzp.io" in x for x in allof)


def test_recall_old_chat_with_nothing_earlier(world):
    result, _, _ = run(world, "recall_old_chat", {})
    assert result["facts"]["messages"] == [] and "no earlier chat" in result["facts"]["info"]


# --------------------------------------------------------------------------- the schemas and the dispatcher


def test_there_are_12_schemas_that_say_when_to_use_each_tool():
    names = [x.name for x in TOOL_SCHEMAS]
    assert names == ["update_enquiry", "get_price", "send_photos", "send_map", "create_booking", "check_payment",
                     "send_payment_link", "my_bookings", "change_name", "talk_to_owner", "repeat_step",
                     "recall_old_chat"]
    assert tools_module.TOOLS is TOOL_SCHEMAS
    for spec in TOOL_SCHEMAS:
        assert spec.description.startswith("Use this") and len(spec.description) > 60, spec.name  # WHEN, not how
        assert spec.parameters["type"] == "object"
        assert set(spec.parameters.get("required", [])) <= set(spec.parameters["properties"]), spec.name
    by = {x.name: x for x in TOOL_SCHEMAS}
    assert by["send_payment_link"].parameters["properties"]["reason"]["enum"] == ["lost", "hold", "not_paid_yet"]
    assert by["create_booking"].parameters["required"] == ["mobile", "language"]  # the name may be empty


def test_every_tool_has_a_handler_and_an_unknown_one_is_a_normal_result(world):
    w = world
    for spec in TOOL_SCHEMAS:
        assert callable(getattr(AgentTools, spec.name)), spec.name
    result, texts, _ = run(w, "no_such_tool")
    assert set(result) == SHAPE and not result["ok"] and texts == {}


def test_every_tool_result_has_the_same_shape_and_no_booking_id_or_link(world):
    w = world
    book(w, live=True)
    b = w.repo.list_bookings(phone=GUEST)[0]
    w.pay.paid_links[b.payment_link_id] = "pay_1"
    w.bot._pay_checked = {}
    calls = [("update_enquiry", STAY), ("update_enquiry", {**STAY, "adults": 4}), ("get_price", {}),
             ("send_photos", {"kind": "default"}), ("send_map", {}), ("send_payment_link", {"reason": "lost"}),
             ("check_payment", {}), ("my_bookings", {}), ("change_name", {"name": "Asha"}),
             ("talk_to_owner", {"reason": "x"}), ("recall_old_chat", {}), ("create_booking", {"mobile": "same"})]
    for name, args in calls:
        result, _, _ = run(w, name, args)
        assert set(result) == SHAPE and isinstance(result["ok"], bool), name
        blob = json.dumps(result)
        assert "RB-" not in blob and not any(i in blob for i in all_booking_ids(w)), name
        assert "rzp.io" not in blob and "http" not in blob, name
