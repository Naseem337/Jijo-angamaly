"""Smoke suite for the AI-first resort agent (2026-11-27): ~110 fast FAKE-LLM tests, 1-3 per CURRENT rule.

Run only this file:  .venv\\Scripts\\python.exe -m pytest tests/resort/test_smoke_agent.py -q
No real AI, no network. Tools run live against the scratch DB (so 'once per conversation' rules see the chat log).
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta

import pytest
from test_agent_gate import agent  # noqa: F401  (fixture: AGENT_MODE=agent)
from test_agent_photos import PNG, pw  # noqa: F401  (pw: the resort with a glimpse + photos)
from test_bot import GUEST, STAFF, answer, tool, world  # noqa: F401  (world is a fixture)

from app.providers.llm import LLMResponse, ToolCall
from app.resort import photos as ph
from app.resort import web
from app.resort.agent import gate, guard, holds, late_pay
from app.resort.agent import photos_plan as pp
from app.resort.agent.facts import build_facts
from app.resort.agent.language import TurnLanguage
from app.resort.agent.mobile import check as check_mobile
from app.resort.agent.outbox import bot_outbox
from app.resort.agent.prompt import SYSTEM, today_block
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import AgentTools, build_check_in, build_check_out
from app.resort.agent.turn import split_reply
from app.resort.config import load_resort_config
from app.resort.i18n import t
from app.resort.models import Booking, ChatMessage, RoomType
from app.whatsapp.parse import InboundMessage

STAY = {"place": "Alleppey", "check_in": "2026-11-10", "check_out": "2026-11-12", "adults": 2}
STAY3 = {**STAY, "adults": 3, "rooms": 1}
ASK = "Alleppey 10 nov to 12 nov 2 persons"
NUMBERS = ("9497861924", "9995855789")
_ids = itertools.count(1)


# --------------------------------------------------------------------------- helpers


def call(w, name, args=None, said="", tools=None, box=None):
    """One tool call inside a guest turn, sent live. `said` is the guest's message (stored first, as the gate does)."""
    if said:
        w.repo.add_chat(ChatMessage(GUEST, "in", said, w.clock.now(), "text"))
    box = box or bot_outbox(w.bot, GUEST, dry_run=False)
    tools = tools or AgentTools(w.bot, GUEST, box, guest_text=said or None)
    result = tools.call(name, {"language": "en", **(args or {})})
    box.flush()
    return result, [(i.key, i.text or "", list(i.refs)) for i in box.sent if i.kind != "marker"]


def keys(sent):
    return [k for k, _, _ in sent if not k.startswith("wait")]


def say(w, text, *script, kind="text", **kw):
    """One WhatsApp message through the gate with a scripted fake AI. Returns (outcome, the guest's new texts)."""
    w.llm.script.clear()
    w.llm.script.extend(script)
    before = len(w.wa.texts(GUEST))
    out = w.bot.handle(InboundMessage(f"wamid.smoke{next(_ids)}", GUEST, kind, datetime(2026, 11, 2, 4, 30), text=text,
                                      **kw))
    return out, w.wa.texts(GUEST)[before:]


def reply_args(**args):
    return LLMResponse("", [ToolCall("reply_x", "reply", args)], "tool_calls", provider="mock", model="mock")


def book(w, stay=STAY, name="Asha", mobile="same"):
    call(w, "update_enquiry", stay, said=ASK)
    return call(w, "create_booking", {"guest_name": name, "mobile": mobile}, said=f"{name} {mobile}")


def held(w):
    return [b for b in w.repo.list_bookings(phone=GUEST) if b.status == "hold"]


def pay_latest(w):
    b = held(w)[0]
    w.pay.paid_links[b.payment_link_id] = "pay_1"
    return call(w, "check_payment", said="I paid")


def hold_row(w, ci, co, phone=GUEST, rt=None):
    now = w.clock.now()
    b = Booking(w.p1.id, (rt or w.deluxe).id, phone, ci, co, 2, 0, 1, 8000, 4000, now,
                hold_expires_at=now + timedelta(minutes=9))
    w.repo.create_hold(b, now)
    return b


def status_of(w, booking_id):
    return w.repo.get_booking(booking_id).status


# --------------------------------------------------------------------------- A. greeting / restart / reset all


def test_hi_gets_the_owners_greeting(agent):
    assert say(agent, "hi", answer("Hi, How may I assist you?")) == ("answered", ["Hi, How may I assist you?"])


def test_restart_greets_in_code_with_no_ai_call(agent):
    assert say(agent, "restart") == ("restart", ["Hi, How may I assist you?"]) and not agent.llm.calls


def _paid_then_hold(w):
    book(w)
    pay_latest(w)
    w.clock.advance(minutes=1)
    call(w, "update_enquiry", {**STAY, "check_in": "2026-11-20", "check_out": "2026-11-22"}, said="Alleppey 20 to 22 nov")
    call(w, "create_booking", {"guest_name": "Asha", "mobile": "same"}, said="Asha same")
    return next(b for b in w.repo.list_bookings(phone=GUEST) if b.status == "paid"), held(w)[0]


def test_restart_keeps_the_paid_booking_and_closes_the_unpaid_link(agent):
    paid, hold = _paid_then_hold(agent)
    say(agent, "restart")
    assert status_of(agent, paid.id) == "paid" and status_of(agent, hold.id) != "hold"
    assert hold.payment_link_id in agent.pay.cancelled


def test_reset_all_for_a_tester_keeps_paid_bookings(agent):
    paid, hold = _paid_then_hold(agent)
    agent.bot.config = agent.bot.config.model_copy(update={"agent_test_phones": [GUEST]})
    assert say(agent, "reset all") == ("reset_all", ["Reset done."])
    assert [b.id for b in agent.repo.list_bookings(phone=GUEST)] == [paid.id]
    assert agent.repo.get_guest(GUEST) is not None  # the paid booking keeps its guest record


def test_reset_all_from_a_non_tester_is_a_normal_message(agent):
    out, _ = say(agent, "reset all", answer("Sure."))
    assert out != "reset_all" and len(agent.llm.calls) == 1


def test_a_command_word_inside_a_sentence_goes_to_the_ai(agent):
    out, _ = say(agent, "restart please", answer("Sure."))
    assert out == "answered" and len(agent.llm.calls) == 1


def test_stop_and_start_are_exact_commands(agent):
    assert say(agent, "STOP")[0] == "opt_out" and agent.repo.get_state(GUEST).get("opted_out")
    assert say(agent, "START")[0] == "opt_in" and not agent.repo.get_state(GUEST).get("opted_out")


def test_the_same_whatsapp_message_twice_is_answered_once(agent):
    msg = InboundMessage("wamid.smoke-dup", GUEST, "text", datetime(2026, 11, 2, 4, 30), text="hi")
    agent.llm.script.append(answer("Hi, How may I assist you?"))
    agent.bot.handle(msg)
    assert agent.bot.handle(msg) == "duplicate"


# --------------------------------------------------------------------------- B. language + voice


def test_the_ais_language_is_saved_for_the_turn(agent):
    say(agent, "room venam", answer("Sheri", language="manglish"))
    assert agent.repo.get_state(GUEST)["agent_lang"]["style"] == "manglish"


def test_a_missing_language_keeps_the_last_turns(agent):
    say(agent, "room venam", answer("Sheri", language="manglish"))
    say(agent, "ok", answer("Okay 👍"))
    assert agent.repo.get_state(GUEST)["agent_lang"]["style"] == "manglish"


def test_restart_drops_the_language(agent):
    say(agent, "room venam", answer("Sheri", language="manglish"))
    say(agent, "restart")
    assert Enquiry.load(agent.repo, GUEST).language == "en"


def test_a_language_the_guest_asked_for_wins_over_the_ais():
    lang = TurnLanguage("c")
    lang.set_lock("en")
    assert lang.take("manglish") == "en" and lang.now == "en"


def test_a_malayalam_voice_note_reaches_the_ai_with_its_language(agent, monkeypatch):
    monkeypatch.setattr(gate, "_transcribe", lambda bot, msg: ("എനിക്ക് ഒരു റൂം വേണം", "", ("ml-IN", 0.93)))
    say(agent, "", answer("Sheri", language="manglish"), kind="audio", media_id="m1")
    seen = agent.llm.calls[0].messages[-1].content
    assert "എനിക്ക് ഒരു റൂം വേണം" in seen and "ml-IN" in seen and "Manglish" in seen


def test_a_hindi_voice_note_reaches_the_ai_with_its_language(agent, monkeypatch):
    monkeypatch.setattr(gate, "_transcribe", lambda bot, msg: ("मुझे कमरा चाहिए", "", ("hi-IN", 0.9)))
    say(agent, "", answer("Theek hai", language="hinglish"), kind="audio", media_id="m2")
    seen = agent.llm.calls[0].messages[-1].content
    assert "मुझे कमरा चाहिए" in seen and "hi-IN" in seen and "Hinglish" in seen


def test_a_voice_note_not_understood_gets_the_fixed_text_and_no_ai(agent, monkeypatch):
    monkeypatch.setattr(gate, "_transcribe", lambda bot, msg: ("", "empty", ("", None)))
    assert say(agent, "", kind="audio", media_id="m3")[0] == "voice_problem" and not agent.llm.calls


# --------------------------------------------------------------------------- C. dates


@pytest.mark.parametrize("parts,want", [
    ((20, None, None), date(2026, 12, 20)),  # a day alone that passed this month -> next month
    ((31, None, None), date(2026, 12, 31)),  # November has no 31st
    ((20, 11, None), date(2026, 11, 20)),  # no year, passed 5 days ago: that date (past_date is told)
    ((10, 1, None), date(2027, 1, 10)),  # no year, more than 30 days ago -> next year
    ((12, 12, None), date(2026, 12, 12)),
])
def test_check_in_is_built_from_the_guests_words(parts, want):
    assert build_check_in(parts, date(2026, 11, 25))[0] == want


@pytest.mark.parametrize("parts,check_in,want", [
    ((14, None, None), date(2026, 12, 12), date(2026, 12, 14)),  # the check-in's month
    ((2, None, None), date(2026, 11, 28), date(2026, 12, 2)),  # would not be after the check-in -> next month
])
def test_check_out_with_no_month(parts, check_in, want):
    assert build_check_out(parts, check_in) == want


def test_relative_dates_are_worked_out_in_the_today_block():
    block = today_block(date(2026, 11, 25))
    assert "{day: 26, month: 11, year: 2026}" in block and "{day: 27, month: 11, year: 2026}" in block
    assert "naale" in block and "परसों" in block


# --------------------------------------------------------------------------- D. adults


def test_the_prompt_counts_the_guest_and_never_children():
    assert '"me and 2 friends" = 3' in SYSTEM and "never children" in SYSTEM


def test_adults_alone_get_2_per_room(world):
    res, _ = call(world, "update_enquiry", {**STAY, "adults": 5}, said="Alleppey 10 nov to 12 nov 5 adults")
    assert res["facts"]["rooms"] == 3


def test_never_more_than_3_adults_in_a_room(world):
    res, _ = call(world, "update_enquiry", {**STAY, "adults": 4, "rooms": 1}, said="Alleppey 10-12 nov 4 adults 1 room")
    assert res["facts"]["rooms"] == 2


# --------------------------------------------------------------------------- E. the form split from the AI's text

FORM = "Please share these details:\n\nCheck in :\nCheck out :\nNumber of adults (above 6 years) :\nNumber of rooms:"
CONTACT = "Please share your name and mobile number to proceed."


def test_split_reply_cuts_answer_form_and_contact_ask():
    assert split_reply(f"Yes, the pool is open.\n{FORM}\n{CONTACT}") == ["Yes, the pool is open.", FORM, CONTACT]


def test_the_answer_and_the_form_go_as_two_messages(agent):
    _, texts = say(agent, "is there a pool", answer(f"Yes, there is a pool.\n{FORM}"))
    assert texts == ["Yes, there is a pool.", FORM]


def test_the_same_form_is_not_sent_again_within_3_messages(agent):
    say(agent, "is there a pool", answer(f"Yes, there is a pool.\n{FORM}"))
    _, texts = say(agent, "till when", answer(f"The pool is open till 9.\n{FORM}"))
    assert texts == ["The pool is open till 9."]


# --------------------------------------------------------------------------- F. availability line + order


def test_the_availability_line_comes_first_alone_and_no_price_or_ask_from_code(world):
    _, sent = call(world, "update_enquiry", STAY, said=ASK)
    assert sent[0][0] == "rooms_ok" and sent[0][1].split("\n")[0] == "Yes, rooms are available for your dates."
    assert "price" not in keys(sent) and "ask_contact" not in keys(sent)


def test_after_a_room_check_glimpse_general_photos_then_the_rooms(pw):
    _, sent = call(pw, "update_enquiry", STAY, said=ASK)
    assert keys(sent) == ["rooms_ok", "glimpse", "photos:3", "room_label", "photos:2"]
    assert dict((k, x) for k, x, _ in sent)["room_label"] == "*Deluxe*"


def _numbered(w):
    rt = w.repo.get_room_type(w.deluxe.id)
    own = {"101": [ph.save_photo(PNG + b"own101")], "102": [ph.save_photo(PNG + b"own102")]}
    w.repo.save_room_type(RoomType(**{**rt.__dict__, "room_list": ["101", "102"], "number_photos": own}))
    return own


def test_shared_room_type_photos_go_once_then_each_rooms_own(pw):
    own = _numbered(pw)
    _, sent = call(pw, "update_enquiry", {**STAY, "adults": 4, "rooms": 2}, said="Alleppey 10-12 nov 4 adults 2 rooms")
    labels = [(text, sent[i + 1][2]) for i, (k, text, _) in enumerate(sent) if k == "room_label"]
    assert labels == [("*Room 101 - Deluxe*", pw.refs["deluxe"] + own["101"]), ("*Room 102 - Deluxe*", own["102"])]


def test_the_glimpse_and_general_photos_never_go_twice(pw):
    call(pw, "update_enquiry", STAY, said=ASK)
    _, sent = call(pw, "update_enquiry", {**STAY, "check_in": "2026-11-20", "check_out": "2026-11-22"},
                   said="Alleppey 20 to 22 nov")
    assert "glimpse" not in keys(sent) and not any(r in pw.general for _, _, refs in sent for r in refs)


def test_label_sets_put_the_shared_photos_under_the_first_label():
    rt = RoomType("P", "Deluxe", room_list=["101", "102"], photo_urls=["s"], number_photos={"101": ["a"], "102": ["b"]})
    assert pp.label_sets(rt, ["s", "a", "b"], "102") == [("*Room 102 - Deluxe*", ["s", "b"]),
                                                          ("*Room 101 - Deluxe*", ["a"])]


# --------------------------------------------------------------------------- G. photos vs availability


def test_photos_before_dates_get_the_form_not_photos(pw):
    call(pw, "update_enquiry", {"place": "Alleppey"}, said="Alleppey")
    res, sent = call(pw, "send_photos", {"kind": "default", "purpose": "see"}, said="photos")
    assert res["facts"]["dates_missing"] and res["facts"]["photos"] == 0 and sent == []


def test_photos_insisted_on_before_dates_are_sent(pw):
    call(pw, "update_enquiry", {"place": "Alleppey"}, said="Alleppey")
    res, sent = call(pw, "send_photos", {"kind": "default", "purpose": "see", "insist": True}, said="just send photos")
    assert res["facts"]["photos"] > 0 and any(k.startswith("photos:") for k in keys(sent))


def test_photos_to_see_never_claim_availability(pw):
    call(pw, "update_enquiry", STAY, said=ASK)
    res, _ = call(pw, "send_photos", {"kind": "default", "purpose": "see"}, said="more photos")
    assert res["facts"].get("availability_checked") is False and "available" not in res["facts"]


def test_considering_another_room_says_it_is_also_free(pw):
    call(pw, "update_enquiry", STAY, said=ASK)
    _, sent = call(pw, "send_photos", {"kind": "room_type", "room_type": "family", "purpose": "consider"},
                   said="is the family suite free?")
    assert "also_free" in keys(sent) and "Family Suite" in dict((k, x) for k, x, _ in sent)["also_free"]


# --------------------------------------------------------------------------- H. room-name matching


@pytest.mark.parametrize("said,want", [("family", "Family Suite"), ("delux", "Deluxe"), ("cottage", None)])
def test_room_names_are_matched_loosely_and_unknown_ones_refused(world, said, want):
    call(world, "update_enquiry", STAY, said=ASK)
    res, _ = call(world, "update_enquiry", {**STAY, "room_picks": [{"room_type": said, "rooms": 1}]},
                  said=f"I want {said}")
    if want is None:
        assert not res["ok"] and "no room type 'cottage'" in res["facts"]["problem"]
    else:
        assert res["facts"]["picked_rooms_check"][0]["room_type"] == want


# --------------------------------------------------------------------------- I. no availability claim without a tool


def test_a_free_claim_without_a_tool_is_flagged():
    assert guard.sentence_problem("The Deluxe room is available for your dates.", guard.Sources())


def test_a_free_claim_after_a_tool_found_a_room_is_fine():
    src = guard.Sources(free_now=True, checked_now=True)
    assert guard.sentence_problem("The Deluxe room is available for your dates.", src) == ""


def test_a_not_free_claim_without_a_check_is_flagged():
    assert guard.sentence_problem("Sorry, the Deluxe room is not available.", guard.Sources())


# --------------------------------------------------------------------------- J. price


def test_the_price_block_goes_only_from_get_price(world):
    call(world, "update_enquiry", STAY, said=ASK)
    res, sent = call(world, "get_price", said="price?")
    assert res["facts"]["price_block_sent"] and keys(sent) == ["price"] and "*Total: ₹8,000*" in sent[0][1]


def test_get_price_without_dates_asks_the_form_with_the_price_reason(world):
    call(world, "update_enquiry", {"place": "Alleppey"}, said="Alleppey")
    res, sent = call(world, "get_price", said="price?")
    assert res["facts"]["form_reason"] == "price" and sent == []


def test_the_summary_shows_the_room_cost_per_night(world):
    _, sent = book(world)
    assert "Room Cost - 4,000/Night" in dict((k, x) for k, x, _ in sent)["summary"]


def test_an_invented_amount_is_flagged():
    assert "₹2,999" in guard.sentence_problem("It is ₹2,999 a night.", guard.Sources(code="total 8000"))


# --------------------------------------------------------------------------- K. cheaper rooms, no season talk


def test_compare_rooms_offers_the_cheaper_free_room(world):
    call(world, "update_enquiry", {**STAY, "room_picks": [{"room_type": "Family Suite", "rooms": 1}]}, said=ASK)
    res, _ = call(world, "compare_rooms", said="cheaper room?")
    assert [(o["room_type"], o["total"]) for o in res["facts"]["cheaper_options"]] == [("Deluxe", 8000)]


def test_compare_rooms_says_already_cheapest(world):
    call(world, "update_enquiry", STAY, said=ASK)
    res, _ = call(world, "compare_rooms", said="cheaper?")
    assert res["facts"]["already_cheapest"] is True


def test_no_reason_for_a_price_is_ever_given(world):
    assert "Never say why a price is what it is (season, peak, weekend)" in SYSTEM
    call(world, "update_enquiry", STAY, said=ASK)
    _, sent = call(world, "get_price", said="why so costly")
    assert not any(w in sent[0][1].lower() for w in ("season", "peak", "weekend rate"))


# --------------------------------------------------------------------------- L. extra bed


def test_3_adults_in_one_room_get_the_extra_bed_question_first(world):
    res, sent = call(world, "update_enquiry", STAY3, said="Alleppey 10-12 nov 3 adults 1 room")
    assert res["facts"]["extra_bed_question"] is True and "ask_contact" not in keys(sent)


def test_extra_bed_yes_gives_the_rooms_line_and_the_name_ask(world):
    call(world, "update_enquiry", STAY3, said="Alleppey 10-12 nov 3 adults 1 room")
    _, sent = call(world, "update_enquiry", {**STAY3, "extra_beds": 1}, said="yes")
    assert keys(sent) == ["rooms_line", "ask_contact"] and "1 extra bed" in sent[0][1]


def test_extra_bed_no_gives_the_rooms_line_without_a_bed(world):
    call(world, "update_enquiry", STAY3, said="Alleppey 10-12 nov 3 adults 1 room")
    _, sent = call(world, "update_enquiry", {**STAY3, "extra_beds": 0}, said="no")
    assert [x for _, x, _ in sent] == ["For 3 persons: 1 room(s).", CONTACT]


def test_a_lone_ok_after_code_answered_is_not_sent(agent):
    say(agent, "Alleppey 10 nov to 12 nov 3 adults 1 room", tool("update_enquiry", **STAY3, language="en"),
        answer("Do you need an extra bed?"))
    _, texts = say(agent, "yes", tool("update_enquiry", **STAY3, extra_beds=1, language="en"), answer("Ok."))
    assert "Ok." not in texts and texts[-1] == CONTACT


# --------------------------------------------------------------------------- M. big group

BIG = {**STAY, "adults": 30}


def test_a_big_group_gets_the_owners_text_and_one_team_alert(world):
    res, sent = call(world, "update_enquiry", BIG, said="Alleppey 10 to 12 nov 30 people")
    assert res["facts"]["group_too_big"] and res["facts"]["free_rooms"] == 5 and "group_too_big" in keys(sent)
    assert sum("Big group" in x for x in world.wa.texts(STAFF)) == 1


def test_the_big_group_text_and_alert_go_once(world):
    call(world, "update_enquiry", BIG, said="Alleppey 10 to 12 nov 30 people")
    res, sent = call(world, "update_enquiry", BIG, said="ok")
    assert res["facts"]["group_message_already_sent"] and sent == []
    assert sum("Big group" in x for x in world.wa.texts(STAFF)) == 1


def test_the_big_group_question_is_given_to_the_ai(world):
    res, _ = call(world, "update_enquiry", BIG, said="Alleppey 10 to 12 nov 30 people")
    assert "Do you want to book the available 5 rooms for 15 people?" in res["note"]


def test_accepting_the_free_rooms_books_them(world):
    call(world, "update_enquiry", BIG, said="Alleppey 10 to 12 nov 30 people")
    res, _ = call(world, "update_enquiry", {**BIG, "accept_free_rooms": True}, said="book the available rooms")
    assert res["facts"]["available"] is True and res["facts"]["rooms"] == 5


def test_ok_is_not_acceptance_in_the_prompt():
    assert '"ok" / "fine" / "hmm" / "thanks" are NOT a yes' in SYSTEM


# --------------------------------------------------------------------------- N. a change before payment


def test_a_date_change_closes_the_old_link_and_sends_a_new_one(world):
    book(world)
    old = held(world)[0]
    res, sent = call(world, "update_enquiry", {**STAY, "check_in": "2026-11-20", "check_out": "2026-11-22",
                                               "change_existing": True}, said="change to 20 to 22 nov")
    assert res["facts"]["booking_updated"] and res["facts"]["old_link_closed"]
    assert old.payment_link_id in world.pay.cancelled and keys(sent) == ["summary", "pay"]


def test_after_a_change_there_is_one_hold(world):
    book(world)
    call(world, "update_enquiry", {**STAY, "check_in": "2026-11-20", "check_out": "2026-11-22",
                                   "change_existing": True}, said="change to 20 to 22 nov")
    assert len(held(world)) == 1 and held(world)[0].check_in == date(2026, 11, 20)


def test_the_same_stay_as_the_link_changes_nothing(world):
    book(world)
    _, sent = call(world, "update_enquiry", STAY, said="10 to 12 nov 2 persons")
    assert sent == [] and len(held(world)) == 1


def test_a_mixed_stay_is_one_hold(world):
    book(world, {**STAY, "adults": 5})
    rows = held(world)
    assert len(rows) == 2 and holds.keep_newest(world.bot, GUEST) == [] and len(held(world)) == 2


def test_an_older_hold_on_other_dates_is_kept(world):
    old = hold_row(world, date(2026, 11, 10), date(2026, 11, 12))
    world.clock.advance(minutes=1)
    hold_row(world, date(2026, 12, 20), date(2026, 12, 22))
    assert holds.keep_newest(world.bot, GUEST) == [] and status_of(world, old.id) == "hold"


def test_an_older_overlapping_hold_is_cancelled(world):
    old = hold_row(world, date(2026, 11, 10), date(2026, 11, 12))
    world.clock.advance(minutes=1)
    hold_row(world, date(2026, 11, 11), date(2026, 11, 13), rt=world.family)
    assert holds.keep_newest(world.bot, GUEST) == [old.id] and status_of(world, old.id) == "cancelled"


# --------------------------------------------------------------------------- O. a change after payment


def test_a_paid_booking_is_never_changed_by_the_bot(world):
    book(world)
    pay_latest(world)
    res, sent = call(world, "update_enquiry", {**STAY, "check_in": "2026-11-25", "check_out": "2026-11-27",
                                               "change_existing": True}, said="change to 25 nov")
    assert not res["ok"] and "paid_booking_cannot_be_changed" in res["facts"] and sent == []


def test_a_paid_change_goes_to_the_team_with_the_note(world):
    book(world)
    pay_latest(world)
    call(world, "talk_to_owner", {"reason": "change_or_cancel_paid", "staff_note": "move to 25 nov"}, said="change")
    assert any("PAID booking: move to 25 nov" in x for x in world.wa.texts(STAFF))


# --------------------------------------------------------------------------- P. the payment link policy


def test_p01_name_and_mobile_give_the_summary_then_the_link(world):
    _, sent = book(world)
    assert keys(sent) == ["summary", "pay"] and "https://rzp.io/mock/" in sent[1][1]


def test_p02_the_ai_never_writes_a_payment_url():
    url = "https://rzp.io/mock/plink_1"
    assert "http" not in guard.clean(f"Pay here {url} now.", guard.Sources(code=url))


def test_p03_a_link_from_facts_may_be_written():
    url = "https://maps.app.goo.gl/abc"
    assert url in guard.clean(f"Here is the map: {url}", guard.Sources(code=url, facts=f"Map: {url}"))


def test_p04_hold_sends_the_hold_text(world):
    book(world)
    _, sent = call(world, "send_payment_link", {"reason": "hold"}, said="hold it, I pay tonight")
    assert keys(sent) == ["hold_pay"]


def test_p05_the_hold_text_goes_once_then_the_plain_link(world):
    book(world)
    call(world, "send_payment_link", {"reason": "hold"}, said="hold it")
    _, sent = call(world, "send_payment_link", {"reason": "hold"}, said="hold it please")
    assert keys(sent) == ["pay"]


def test_p06_a_lost_link_sends_the_same_link(world):
    book(world)
    _, sent = call(world, "send_payment_link", {"reason": "lost"}, said="send the link again")
    assert keys(sent) == ["pay"] and held(world)[0].payment_link_url in sent[0][1]


def test_p07_no_link_no_message(world):
    res, sent = call(world, "send_payment_link", {"reason": "lost"}, said="link?")
    assert res["facts"]["open_link"] is False and sent == []


def test_p08_a_link_after_the_hold_ended_is_renewed(world):
    book(world)
    old = held(world)[0].payment_link_id
    world.clock.advance(minutes=10)
    web.agent_sweep(world.bot)
    _, sent = call(world, "send_payment_link", {"reason": "lost"}, said="I am ready to pay now")
    assert "pay" in keys(sent) and held(world) and held(world)[0].payment_link_id != old


def test_p09_one_link_send_per_turn(world):
    book(world)
    box = bot_outbox(world.bot, GUEST, dry_run=False)
    tools = AgentTools(world.bot, GUEST, box, guest_text="link")
    call(world, "send_payment_link", {"reason": "lost"}, tools=tools, box=box)
    res, _ = call(world, "send_payment_link", {"reason": "lost"}, tools=tools, box=box)
    assert res["facts"]["already_done"] is True


def test_p10_the_hold_end_cancels_the_link_and_frees_the_rooms(world):
    book(world)
    b = held(world)[0]
    world.clock.advance(minutes=10)
    out = web.agent_sweep(world.bot)
    assert out["links_closed"] == 1 and b.payment_link_id in world.pay.cancelled and status_of(world, b.id) == "expired"


def test_p11_one_reminder_before_the_hold_ends(world):
    book(world)
    world.clock.advance(minutes=7)
    assert web.remind_once(world.bot, world.clock.now()) == 1 and web.remind_once(world.bot, world.clock.now()) == 0


def test_p12_a_pay_ask_without_a_matching_link_is_removed():
    src = guard.Sources(pay_ok=False)
    assert guard.clean("Please pay the advance using the link above. The pool is open.", src) == "The pool is open."


def test_p13_a_pay_ask_with_a_matching_link_stays():
    text = "Please pay the advance using the link above."
    assert guard.clean(text, guard.Sources(pay_ok=True)) == text


def test_p14_a_new_booking_replaces_the_older_hold(world):
    book(world)
    first = held(world)[0]
    world.clock.advance(minutes=1)
    call(world, "create_booking", {"guest_name": "Asha", "mobile": "same"}, said="Asha same")
    assert status_of(world, first.id) == "cancelled" and len(held(world)) == 1


def test_p15_a_paid_booking_is_never_touched_by_the_cleanup(world):
    book(world)
    pay_latest(world)
    paid = next(b for b in world.repo.list_bookings(phone=GUEST) if b.status == "paid")
    world.clock.advance(minutes=1)
    hold_row(world, date(2026, 11, 10), date(2026, 11, 12), rt=world.family)
    holds.keep_newest(world.bot, GUEST)
    assert status_of(world, paid.id) == "paid"


def test_p16_the_hold_is_9_minutes():
    assert load_resort_config().hold_minutes == 9


# --------------------------------------------------------------------------- Q. "I paid"


def test_i_paid_and_it_is_paid_confirms(world):
    book(world)
    res, sent = pay_latest(world)
    assert res["facts"]["paid"] is True and "confirmed" in keys(sent)


def test_i_paid_but_it_is_not_paid(world):
    book(world)
    res, sent = call(world, "check_payment", said="I paid")
    assert res["facts"]["paid"] is False and sent == []


def test_i_paid_with_no_booking(world):
    res, _ = call(world, "check_payment", said="I paid")
    assert res["facts"]["no_booking"] is True


def test_i_paid_again_after_the_confirmation_says_already_confirmed(world):
    book(world)
    pay_latest(world)
    res, _ = call(world, "check_payment", said="did you get it?")
    assert res["facts"].get("already_paid") is True


# --------------------------------------------------------------------------- R. a late payment


def test_a_late_payment_with_rooms_still_free_is_confirmed(world):
    book(world)
    b = held(world)[0]
    world.clock.advance(minutes=10)
    web.agent_sweep(world.bot)
    assert late_pay.mark_paid(world.bot, booking_id=b.id, payment_id="p1") == "paid" and status_of(world, b.id) == "paid"


def test_a_late_payment_with_rooms_gone_alerts_the_team(world):
    book(world, {**STAY, "adults": 4, "rooms": 2})
    b = held(world)[0]
    world.clock.advance(minutes=10)
    web.agent_sweep(world.bot)
    for _ in range(2):
        other = Booking(world.p1.id, b.room_type_id, "+919811111111", b.check_in, b.check_out, 2, 0, 1, 8000, 4000,
                        world.clock.now(), hold_expires_at=world.clock.now() + timedelta(minutes=9))
        other.status = "paid"
        world.repo.create_hold(other, world.clock.now())
    assert late_pay.mark_paid(world.bot, booking_id=b.id, payment_id="p2") == "late_rooms_gone"
    assert any("Late payment" in x for x in world.wa.texts(STAFF)) and late_pay.GUEST_TEXT in world.wa.texts(GUEST)


# --------------------------------------------------------------------------- S. the mobile number


@pytest.mark.parametrize("raw,ok", [("9847011111", True), ("same", True), ("+447911123456", True),
                                    ("9999999999", False), ("9888888881", False), ("9888888812", True)])
def test_mobile_numbers_are_checked_in_code(raw, ok):
    assert bool(check_mobile(raw)) is ok


def test_three_wrong_numbers_give_the_team_numbers(world):
    call(world, "update_enquiry", STAY, said=ASK)
    for _ in range(3):
        res, sent = call(world, "create_booking", {"guest_name": "Ravi", "mobile": "1111111111"}, said="Ravi 1111111111")
    assert res["facts"]["wrong_numbers"] == 3 and res["facts"]["numbers"] and sent == []


# --------------------------------------------------------------------------- T. discount


def test_the_discount_ladder_is_in_the_prompt():
    assert "first ask → ONE short line" in SYSTEM and "second → talk_to_owner(other) + handoff" in SYSTEM
    assert '"The price is fixed, sorry."' in SYSTEM


def test_a_second_discount_ask_alerts_the_team(world):
    call(world, "update_enquiry", STAY, said=ASK)
    res, _ = call(world, "talk_to_owner", {"reason": "other", "staff_note": "asks a lower price again"}, said="less?")
    assert res["facts"]["team_told"] and any("asks a lower price again" in x for x in world.wa.texts(STAFF))


# --------------------------------------------------------------------------- U. the team's numbers, at most 2 in 5


def _three_number_turns(w, last):
    say(w, "number?", answer(f"Please call the team on {NUMBERS[0]}."))
    say(w, "another number?", answer(f"You can also reach them on {NUMBERS[1]}."))
    return say(w, "again?", last)[1]


def test_the_team_numbers_a_third_time_are_replaced(agent):
    texts = _three_number_turns(agent, answer(f"Call {NUMBERS[0]} for that."))
    assert texts == ["Call the number shared above for that."]


def test_the_team_numbers_go_when_the_guest_asks_for_them(agent):
    texts = _three_number_turns(agent, reply_args(text=f"Call {NUMBERS[0]} for that.", guest_asked_contact=True))
    assert texts == [f"Call {NUMBERS[0]} for that."]


# --------------------------------------------------------------------------- V. internal talk


def test_an_internal_action_sentence_is_removed(agent):
    assert say(agent, "pool?", answer("I have informed the team. Yes, the pool is open."))[1] == ["Yes, the pool is open."]


def test_the_payment_provider_name_is_removed():
    assert "razorpay" not in guard.clean("It is a secure link via Razorpay.", guard.Sources()).lower()


# --------------------------------------------------------------------------- W. owner answers


def _dining(w):
    w.p1.owner_answers = [{"id": "A1", "topic": "dining", "examples": "food", "text": "We serve Kerala meals."}]
    w.repo.save_property(w.p1)
    call(w, "update_enquiry", {"place": "Alleppey"}, said="Alleppey")


def test_an_owner_answer_goes_word_for_word(world):
    _dining(world)
    _, sent = call(world, "owner_answer", {"topic": "dining"}, said="food available?")
    assert [x for _, x, _ in sent] == ["We serve Kerala meals."]


def test_an_owner_answer_goes_once_per_chat(world):
    _dining(world)
    call(world, "owner_answer", {"topic": "dining"}, said="food available?")
    res, sent = call(world, "owner_answer", {"topic": "dining"}, said="food?")
    assert res["facts"]["already_sent"] and sent == []


# --------------------------------------------------------------------------- X. no internal words like "meal plan"


def test_the_prompt_forbids_meal_plan_words():
    assert '"meal plan", "stay only", EP / CP / MAP / AP' in SYSTEM


def test_facts_mark_the_food_code_word(world):
    world.p1.meal_plan = "Stay Only"
    world.repo.save_property(world.p1)
    line = next(ln for ln in build_facts(world.bot).splitlines() if "Stay Only" in ln)
    assert "never say it to the guest" in line


# --------------------------------------------------------------------------- Y. the guards never leave nothing


def test_a_reply_the_guards_empty_twice_still_answers(agent):
    call(agent, "update_enquiry", STAY, said=ASK)
    out, texts = say(agent, "hmm", answer("Razorpay"), answer("Razorpay"))
    assert out == "fallback" and texts and "say that again" not in texts[0].lower()


def test_an_unreadable_message_gets_say_again(agent):
    out, texts = say(agent, "???", answer(""), answer(""))
    assert out == "say_again" and texts == [t("say_again_short", "en")]


# --------------------------------------------------------------------------- Z. a crash is never silent


def _boom(*args, **kwargs):
    raise RuntimeError("boom")


def test_a_crash_alerts_the_team_and_answers_the_guest(agent, monkeypatch):
    monkeypatch.setattr(gate, "run_turn", _boom)
    out, texts = say(agent, "hello there")
    assert out == "crashed" and texts == [gate.MOMENT]
    assert agent.wa.texts(STAFF)[-1] == "Bot error for +91******2345: RuntimeError: boom"


def test_a_second_crash_does_not_alert_again(agent, monkeypatch):
    monkeypatch.setattr(gate, "run_turn", _boom)
    say(agent, "hello")
    say(agent, "hello again")
    assert sum(x.startswith("Bot error") for x in agent.wa.texts(STAFF)) == 1


def test_a_crash_with_a_booking_under_way_sends_the_open_step(agent, monkeypatch):
    call(agent, "update_enquiry", STAY, said=ASK)
    monkeypatch.setattr(gate, "run_turn", _boom)
    assert say(agent, "ok")[1] == [CONTACT]


def test_a_crash_after_the_reply_went_adds_nothing(agent, monkeypatch):
    def half(bot, llm, phone, text, box, swipe=""):
        box.text("ai", "Yes, the pool is open.")
        box.flush()
        raise RuntimeError("late")
    monkeypatch.setattr(gate, "run_turn", half)
    assert say(agent, "pool?")[1] == ["Yes, the pool is open."]
