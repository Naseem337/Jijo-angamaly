"""Agent fix round 3 (round-2 eval fixes, short human replies, room labels and photos), with the FAKE LLM."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from test_agent_fix_round1 import late_month
from test_agent_gate import agent, m, send  # noqa: F401  (agent is a fixture)
from test_agent_photos import pw  # noqa: F401  (a fixture)
from test_agent_photos import keys
from test_agent_photos import run as prun
from test_agent_tools_2c import book
from test_agent_turn import STAY, go, texts
from test_bot import GUEST, answer, reply, tool, world  # noqa: F401  (world is a fixture)

from app.resort.agent import guard
from app.resort.agent import photos_plan as pp
from app.resort.agent.gate import _restart
from app.resort.agent.outbox import bot_outbox, near_same
from app.resort.agent.prompt import SYSTEM
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import TOOL_SCHEMAS, AgentTools, build_check_in, build_check_out
from app.resort.i18n import t
from app.resort.models import ChatMessage, RoomType

ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 11, 25)


def call(w, name, args, *, said: str | None = "", live=False):
    """One tool call inside a guest turn: `said` is the guest's message (None = outside a turn, places not checked)."""
    if said:
        w.repo.add_chat(ChatMessage(GUEST, "in", said, w.clock.now(), "text"))
    box = bot_outbox(w.bot, GUEST, dry_run=not live)
    result = AgentTools(w.bot, GUEST, box, guest_text=said).call(name, args)
    box.flush()
    return result, {i.key: i.text for i in box.sent}, box


def spec(name):
    return next(s for s in TOOL_SCHEMAS if s.name == name)


# --------------------------------------------------------------------------- 0a) dates: the AI copies, code builds


@pytest.mark.parametrize("parts,want", [
    ((20, None, None), date(2026, 12, 20)),  # a day alone that passed this month: next month
    ((25, None, None), date(2026, 11, 25)),  # today
    ((28, None, None), date(2026, 11, 28)),
    ((31, None, None), date(2026, 12, 31)),  # November has no 31st
    ((20, 11, None), date(2026, 11, 20)),  # no year, passed 5 days ago: that past date (past_date is told)
    ((10, 1, None), date(2027, 1, 10)),  # no year, more than 30 days ago: next year
    ((12, 12, None), date(2026, 12, 12)),
    ((20, 11, 2026), date(2026, 11, 20)),  # a full date stays as given (past_date is told)
])
def test_the_check_in_is_built_from_what_the_guest_said(parts, want):
    assert build_check_in(parts, TODAY)[0] == want


@pytest.mark.parametrize("ci,parts,want", [
    (date(2026, 12, 12), (14, None, None), date(2026, 12, 14)),  # the check-in's month
    (date(2026, 11, 30), (2, None, None), date(2026, 12, 2)),  # before the check-in: the next month
    (date(2026, 12, 30), (2, 1, None), date(2027, 1, 2)),  # month without a year: after the check-in
])
def test_the_check_out_is_built_from_the_check_in(ci, parts, want):
    assert build_check_out(parts, ci) == want


def test_the_date_fields_say_copy_only_what_the_guest_said():
    props = spec("update_enquiry").parameters["properties"]
    assert props["check_in"]["description"].startswith("Copy only what the guest said. Never fill month or year yourself.")
    assert props["check_in"]["required"] == ["day"] and "month_given" not in props


def test_a_day_alone_goes_to_the_next_time_it_comes_with_its_check_out(world):
    late_month(world)  # 2026-11-25
    result, sent, _ = call(world, "update_enquiry", {"place": "Alleppey", "check_in": {"day": 30},
                                                     "check_out": {"day": 2}, "adults": 2}, said="Alleppey 30 to 2")
    assert (result["facts"]["check_in"], result["facts"]["check_out"]) == ("2026-11-30", "2026-12-02")
    assert result["facts"]["available"] is True


def test_nights_still_work_instead_of_check_out(world):
    result, _, _ = call(world, "update_enquiry", {"place": "Alleppey", "check_in": {"day": 10, "month": 12}, "nights": 3,
                                                  "adults": 2}, said="Alleppey dec 10, 3 nights")
    assert (result["facts"]["check_in"], result["facts"]["check_out"]) == ("2026-12-10", "2026-12-13")


def test_a_full_date_that_passed_is_told(world):
    late_month(world)
    result, sent, _ = call(world, "update_enquiry", {"place": "Alleppey", "check_in": {"day": 20, "month": 11, "year": 2026},
                                                     "nights": 1, "adults": 2}, said="Alleppey 20 Nov 2026")
    assert result["sent"] == [] and "has passed" in result["facts"]["problem"]


def test_a_bad_date_object_goes_back_to_the_ai_and_nothing_to_the_guest(world):
    result, sent, _ = call(world, "update_enquiry", {"place": "Alleppey", "check_in": {"day": 40}}, said="Alleppey")
    assert not result["ok"] and sent == {}


# --------------------------------------------------------------------------- 0b) place: only one the guest wrote


def test_a_place_the_guest_never_wrote_is_ignored_so_the_location_question_goes(world):
    result, sent, _ = call(world, "update_enquiry", {"place": "Alleppey"}, said="I need a room")
    assert result["facts"]["missing"] == ["place"] and "Alleppey" in result["facts"]["place_ignored"]
    assert Enquiry.load(world.repo, GUEST).place is None


@pytest.mark.parametrize("said", ["alepey", "Pozhiyoram please", "room in allepy for 2"])
def test_a_misspelt_name_or_alias_the_guest_wrote_counts(world, said):
    result, sent, _ = call(world, "update_enquiry", {"place": "Alleppey"}, said=said)
    assert result["facts"]["form_lines"] and Enquiry.load(world.repo, GUEST).place == world.p1.id


def test_a_place_the_guest_wrote_earlier_in_the_chat_counts(world):
    world.repo.add_chat(ChatMessage(GUEST, "in", "Alleppey", world.clock.now(), "text"))
    result, _, _ = call(world, "update_enquiry", {"place": "Alleppey", "adults": 2}, said="2 adults")
    assert Enquiry.load(world.repo, GUEST).place == world.p1.id


def test_outside_a_guest_turn_the_place_is_not_checked(world):
    result, _, _ = call(world, "update_enquiry", {"place": "Alleppey"}, said=None)
    assert Enquiry.load(world.repo, GUEST).place == world.p1.id


# --------------------------------------------------------------------------- not_our_place


def test_a_town_we_have_no_resort_in_gets_not_our_place_then_the_location_question(world):
    result, sent, _ = call(world, "update_enquiry", {"place": "Kochi"}, said="I am looking for a property in kochi")
    assert result["sent"] == [] and result["facts"]["ours"] is False
    assert result["facts"]["towns"] == "Alleppey and Munnar"  # the AI says "Sorry, we have properties only in ..."


def pp_marker():
    from app.resort.agent.tools import STEP_SHOWN
    return STEP_SHOWN


def test_not_our_place_lists_only_the_open_resorts_from_the_dashboard(world):
    world.p2.active = False
    world.repo.save_property(world.p2)
    for lang in ("en", "mn", "tn", "hn"):
        text = t("not_our_place", lang, towns="Alleppey")
        assert "Alleppey" in text
    result, sent, _ = call(world, "update_enquiry", {"place": "Goa"}, said="goa")
    assert result["facts"]["place_not_ours"] == "Goa" and result["facts"]["towns"] == "Alleppey"  # it goes on
    assert Enquiry.load(world.repo, GUEST).place == world.p1.id


# --------------------------------------------------------------------------- 0c) restart


def test_after_restart_the_ai_sees_its_greeting_and_it_is_not_a_fresh_start(world):
    go(world, "Alleppey 10 to 12 nov 2 adults", tool("update_enquiry", **STAY), reply(""), live=True)
    world.repo.add_chat(ChatMessage(GUEST, "in", "restart", world.clock.now(), "text"))
    _restart(world.bot, GUEST, bot_outbox(world.bot, GUEST))
    res, box = go(world, "I need a room", tool("update_enquiry"), answer("Sure, Kindly confirm the Location"),
                  live=True)
    first = world.llm.calls[-2]
    assert first.messages[0].content == '[You already said: "Hi, How may I assist you?"]\nI need a room'
    assert "same conversation continuing" in first.system
    assert res.calls[0]["result"]["facts"]["missing"] == ["place"] and res.sent == ["ai"]


# --------------------------------------------------------------------------- 0d) language


@pytest.mark.parametrize("given,saved,want", [("Manglish", "en", "manglish"), ("xx", "manglish", "manglish"),
                                              (None, "tanglish", "tanglish")])
def test_a_missing_or_invalid_language_keeps_the_last_one(world, given, saved, want):
    Enquiry(language=saved).save(world.repo, GUEST)
    args = {} if given is None else {"language": given}
    call(world, "update_enquiry", args, said="room venam")
    assert Enquiry.load(world.repo, GUEST).language == want


# --------------------------------------------------------------------------- 0e) 0f) a question not in FACTS


# --------------------------------------------------------------------------- 0g) sir


def test_the_old_send_paths_sir_filter_never_touches_the_first_reply(world):  # 'sir' stays in the FIRST reply
    go(world, "is there a pool?", answer("Yes sir, there is a pool."), live=True)
    assert world.wa.texts(GUEST) == ["Yes sir, there is a pool."]


# --------------------------------------------------------------------------- 0h) the judge


def eval_module(monkeypatch):
    monkeypatch.setenv("RESORT_STAFF_NUMBERS", "+910000000000")  # the eval script sets it on import: restored after
    mod_spec = importlib.util.spec_from_file_location("eval_agent_r3", ROOT / "scripts" / "eval_agent.py")
    ev = importlib.util.module_from_spec(mod_spec)
    monkeypatch.setitem(sys.modules, "eval_agent_r3", ev)
    monkeypatch.setattr(sys, "path", list(sys.path))
    mod_spec.loader.exec_module(ev)
    return ev


def test_the_judge_knows_the_owners_style_rules(monkeypatch):
    ev = eval_module(monkeypatch)
    assert "A short reply is NOT cold." in ev.JUDGE and "sent by code, never by the bot" in ev.JUDGE


def test_the_eval_checks_the_date_code_built_not_the_ais_object(monkeypatch):
    ev = eval_module(monkeypatch)
    calls = [{"name": "update_enquiry", "args": {"check_in": {"day": 20}, "check_out": {"day": 22}},
              "result": {"facts": {"check_in": "2026-12-20", "check_out": "2026-12-22"}}}]
    turn = {"expect_args": {"update_enquiry": {"check_in": "2026-12-20", "check_out": "2026-12-22"}}}
    assert ev.check_turn(turn, "answered", calls, ["rooms_ok"], []) == []
    turn = {"expect_args": {"update_enquiry": {"check_in": "2026-11-20"}}}
    assert ev.check_turn(turn, "answered", calls, ["rooms_ok"], [])


# --------------------------------------------------------------------------- Part 1: short, human replies


def test_a_short_fine_reply_is_not_retried(world):
    res, box = go(world, "is there a pool?", answer("Yes sir, there is a pool."))
    assert not res.retried and texts(box)["ai"] == "Yes sir, there is a pool."


# --------------------------------------------------------------------------- Part 2: rooms, labels, photos


def numbered(w, rt, numbers):
    rt = w.repo.get_room_type(rt.id)
    w.repo.save_room_type(RoomType(**{**rt.__dict__, "room_list": numbers}))


def test_every_offered_room_gets_its_label_even_with_no_photo(world):
    numbered(world, world.deluxe, ["108", "109"])
    result, sent, box = call(world, "update_enquiry", {**STAY, "adults": 4}, said="Alleppey 10 to 12 nov 4 adults")
    labels = [i.text for i in box.sent if i.key == "room_label"]
    assert labels == ["*Room 108 - Deluxe*", "*Room 109 - Deluxe*"]
    assert result["next_step"] == "ask_contact" and "rooms_ok" not in result["sent"]


def test_room_labels_with_different_numbers_are_never_deduped():
    assert not near_same("*Room 108 - Beach View AC Room*", "*Room 109 - Beach View AC Room*")
    assert near_same("Please share your name and mobile number.", "Please share your name and mobile number")


def test_the_guests_own_hold_never_counts_as_taken_for_their_room_number(world):
    numbered(world, world.deluxe, ["108", "109"])
    book(world, live=True)
    held = world.repo.list_bookings(phone=GUEST, status="hold", limit=1)[0]
    held.room_numbers = "108"  # the guest's own hold has room 108
    world.repo.save_booking(held)
    enq = Enquiry.load(world.repo, GUEST)
    rt = world.repo.get_room_type(world.deluxe.id)
    assert pp.next_numbers(world.bot, rt, enq, 1, GUEST) == ["108"]
    assert pp.next_numbers(world.bot, rt, enq, 1) == ["109"]  # anyone else: 108 is held


def test_a_room_the_code_picked_never_gets_room_picked(world):
    call(world, "update_enquiry", STAY, said="Alleppey 10 to 12 nov 2 adults")
    result, sent, _ = call(world, "update_enquiry", {**STAY, "exclude_room_types": ["Deluxe"]}, said="no deluxe")
    assert "room_picked" not in result["facts"] and result["facts"]["rooms_offered"] == "1 Family Suite"
    result, _, _ = call(world, "update_enquiry", {**STAY, "room_picks": [{"room_type": "Deluxe"}]}, said="deluxe please")
    assert result["facts"]["room_picked"] == "Deluxe"


def test_another_room_is_shown_never_switched(pw):  # noqa: F811
    w = pw
    prun(w, "update_enquiry", STAY)
    result, items, _ = prun(w, "send_photos", {"kind": "other_room"})
    assert keys(items)[:3] == ["also_free", "room_label", "photos:2"]
    assert result["facts"]["also_free_for_your_dates"] == "Family Suite"
    assert Enquiry.load(w.repo, GUEST).checked["summary"] == "1 Deluxe"  # the offer is still the Deluxe


def test_the_room_picks_description_keeps_asking_apart_from_choosing():
    desc = spec("update_enquiry").parameters["properties"]["room_picks"]["description"]
    assert "ONLY a clear CHOICE" in desc and "send_photos, never room_picks" in desc
    assert "never switches their room" in spec("send_photos").description


def test_chat_tester_prints_each_photo_as_a_photo_line(capsys, monkeypatch):
    monkeypatch.setenv("RESORT_STAFF_NUMBERS", "")
    monkeypatch.setattr(sys, "path", list(sys.path))
    mod_spec = importlib.util.spec_from_file_location("chat_resort_r3", ROOT / "scripts" / "chat_resort.py")
    cr = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(cr)
    cr.PrintWA(GUEST).send_image(GUEST, "media:abc.png")
    assert capsys.readouterr().out.strip() == "[photo] abc.png"


def test_the_test_data_gives_room_numbers_and_each_rooms_own_photo(world, tmp_path, monkeypatch):
    from app.resort import photos as ph
    from scripts.agent_test_data import add_test_photos
    monkeypatch.setenv("RESORT_DB_PATH", str(tmp_path / "r.sqlite3"))
    monkeypatch.setattr(ph, "_shrink", lambda data: None)
    add_test_photos(world.repo)
    rt = world.repo.get_room_type(world.family.id)
    assert rt.room_list == ["201", "202", "203"] and all(len(rt.number_photos[n]) == 1 for n in rt.room_list)
    assert world.repo.get_property(world.p1.id).photo_urls


_ = (datetime, agent, m, send)  # imported fixtures / helpers kept for pytest
