"""AI-first simplification (2026-11-26): the AI writes to the guest ONLY with reply(); any other text is a private note.
Tools return facts; only the summary, the payment link, the paid confirmation, photos, room labels, the glimpse and the
map are sent by code. The guard keeps 3 checks (amounts from this conversation, 'booked' before payment, provider name
and booking ids) and never replaces a whole answer. FAKE LLM only."""

from __future__ import annotations

from types import SimpleNamespace

from test_agent_fix_round5 import book_live
from test_agent_turn import ASK, STAY, go, texts
from test_bot import GUEST, STAFF, answer, reply, tool, world  # noqa: F401  (world is a fixture)

from app.resort import web
from app.resort.agent import guard
from app.resort.agent.outbox import Outbox, photo_gap
from app.resort.agent.prompt import SYSTEM
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import TOOL_SCHEMAS, AgentTools
from app.resort.agent.turn import REPLY_AGAIN
from app.resort.i18n import t

ASK_CONTACT = "Please share your name and mobile number to proceed."


def sent_texts(box):
    return [i.text for i in box.sent if i.kind == "text"]


# --------------------------------------------------------------------------- 1. reply() is the only way to the guest


def test_only_the_reply_text_reaches_the_guest(world):
    res, box = go(world, "is there parking?", answer("Yes, free parking is available."))
    assert res.sent == ["ai"] and texts(box)["ai"] == "Yes, free parking is available."
    assert res.llm_calls == 1  # a round with reply() ends the turn: no extra AI call


def test_text_outside_reply_is_a_private_note_never_sent(world):
    thinking = "This is the first discount ask, so I'll give a warm line."  # text next to a tool call
    known_place(world)
    res, box = go(world, "can I get it for 8000?",
                  tool("send_map", text=thinking), answer("The rate includes a beach view from every room."))
    assert thinking not in " ".join(sent_texts(box)) and thinking in res.notes
    assert sent_texts(box)[-1] == "The rate includes a beach view from every room."


def test_a_final_text_without_reply_is_a_note_and_the_ai_is_asked_once_more(world):
    res, box = go(world, "what?", reply("The guest is confused, I should explain."),
                  answer("I need your dates to check the rooms."))
    assert res.outcome == "asked_again" and sent_texts(box) == ["I need your dates to check the rooms."]
    assert world.llm.calls[-1].messages[-1].content == REPLY_AGAIN


def test_say_again_only_when_the_ai_is_empty_twice(world):
    res, box = go(world, "hmm", reply(""), reply(""))
    assert res.outcome == "say_again" and res.sent == ["say_again_short"]
    assert texts(box)["say_again_short"] == t("say_again_short", "en")


def known_place(w):
    go(w, "Alleppey", tool("update_enquiry", place="Alleppey"), answer("Please share these details:"))


def test_a_tool_that_sent_something_needs_no_reply(world):
    known_place(world)
    res, box = go(world, "where is it?", tool("send_map"), reply(""))
    assert res.sent == ["map"] and res.outcome == "answered"


def test_reply_may_come_with_other_tool_calls_in_one_round(world):
    from app.providers.llm import LLMResponse, ToolCall
    known_place(world)
    both = LLMResponse("", [ToolCall("a", "send_map", {}), ToolCall("b", "reply", {"text": "Here is the location."})],
                       "tool_calls")
    res, box = go(world, "location?", both)
    assert res.sent == ["map", "ai"] and res.llm_calls == 1


def test_the_greet_and_repeat_step_tools_are_gone_and_reply_is_there():
    names = {s.name for s in TOOL_SCHEMAS}
    assert "reply" in names and "greet" not in names and "repeat_step" not in names


# --------------------------------------------------------------------------- 2. guards on reply() text


def test_an_amount_from_an_earlier_turns_tool_result_is_allowed(world):
    go(world, ASK, tool("update_enquiry", **STAY), reply(""))
    go(world, "price?", tool("get_price"), answer("₹4,000 per room per night, ₹8,000 in total."))
    res, box = go(world, "so how much again?", answer("It is ₹8,000 in total."))  # no tool this turn
    assert res.guard == [] and sent_texts(box) == ["It is ₹8,000 in total."]


def test_an_amount_the_guest_typed_may_be_repeated(world):
    res, box = go(world, "can we get it for 8000?", answer("Sorry, ₹8,000 is not possible for these dates."))
    assert res.guard == [] and sent_texts(box) == ["Sorry, ₹8,000 is not possible for these dates."]


def test_an_unknown_amount_gets_one_retry_with_the_reason(world):
    res, box = go(world, "price?", answer("It is ₹5,500."), answer("Please share your dates first."))
    assert res.retried and "₹5,500" in res.guard[0]
    assert sent_texts(box) == ["Please share your dates first."]
    assert "NOT sent" in res.calls[0]["result"]["note"]


def test_still_wrong_after_the_retry_removes_only_that_sentence(world):
    res, box = go(world, "price?", answer("It is ₹5,500. Please share your dates."),
                  answer("It is ₹5,500 only. Please share your dates."))
    assert sent_texts(box) == ["Please share your dates."]  # never a fallback for a non-empty answer


def test_a_flagged_reply_the_ai_never_retries_goes_without_the_bad_sentence(world):
    res, box = go(world, "price?", answer("Great choice. It is ₹5,500."), reply(""))
    assert sent_texts(box) == ["Great choice."]


def test_booked_before_payment_is_retried_then_its_sentence_removed(world):
    res, box = go(world, "ok", answer("Your room is booked! Enjoy your stay."),
                  answer("Your room is booked. Enjoy your stay."))
    assert res.retried and sent_texts(box) == ["Enjoy your stay."]


def test_booked_is_fine_when_it_says_not_yet(world):
    res, box = go(world, "ok", answer("It is confirmed once the advance is paid."))
    assert res.guard == [] and sent_texts(box) == ["It is confirmed once the advance is paid."]


def test_to_confirm_and_will_be_confirmed_are_the_future_and_fine():
    src = guard.Sources(code="", guest="")
    assert guard.problems("Please pay the advance with the link above to confirm your booking.", src) == []
    assert guard.problems("It will be confirmed automatically.", src) == []
    assert guard.problems("Your booking is confirmed.", src)


def test_the_provider_name_and_booking_ids_are_stripped():
    src = guard.Sources(code="", guest="")
    assert guard.clean("Pay via Razorpay for RB-1A2B3C now.", src) == "Pay for now."
    assert guard.clean("See https://evil.example/x", src) == "See"


def test_no_length_limit_banned_phrase_or_sent_claim_check_is_left(world):
    long = ("Yes, there is a pool. We have a lovely beach view for you. "
            "Would you like to see photos? Feel free to ask anything else. I have sent the details.")
    res, box = go(world, "rooms?", answer(long))
    assert res.guard == [] and sent_texts(box) == [long]
    for gone in ("style_problems", "fix_style", "booking_talk", "repeats_tools", "drop_false_sent", "BANNED"):
        assert not hasattr(guard, gone)


# --------------------------------------------------------------------------- 3. tools return facts, not guest text


def test_missing_details_come_back_as_form_lines_with_nothing_sent(world):
    res, box = go(world, "Alleppey", tool("update_enquiry", place="Alleppey"),
                  answer("Please share these details:\n\nCheck in :\nCheck out :"))
    facts = res.calls[0]["result"]["facts"]
    assert facts["form_lines"] == ["Check in :", "Check out :", "Number of adults (above 6 years) :", "Number of rooms:"]
    assert facts["form_reason"] == "booking" and res.calls[0]["result"]["sent"] == []


def test_get_price_with_no_dates_returns_the_price_reason(world):
    go(world, "Alleppey", tool("update_enquiry", place="Alleppey"), answer("Please share these details:"))
    res, _ = go(world, "let me know the price", tool("get_price"), answer("To check the price, please share these details:"))
    assert res.calls[0]["result"]["facts"]["form_reason"] == "price"


def test_an_available_stay_sends_no_owner_text_only_labels_and_photos(world):
    res, box = go(world, ASK, tool("update_enquiry", **STAY), answer("Yes, rooms are available for your dates.\n" + ASK_CONTACT))
    keys = [k for k in res.sent if k != "ai"]
    assert set(keys) <= {"room_label", "glimpse"} | {k for k in keys if k.startswith("photos:")}
    assert res.calls[0]["result"]["facts"]["available"] is True and res.sent[-1] == "ai"


def test_create_booking_still_sends_the_summary_and_the_link(world):
    go(world, ASK, tool("update_enquiry", **STAY), answer(ASK_CONTACT))
    res, _ = go(world, "Asha, same number", tool("create_booking", guest_name="Asha", mobile="same"), reply(""))
    assert res.sent == ["summary", "pay"]


def test_check_payment_not_paid_is_a_fact_the_ai_tells(world):
    book_live(world)
    res, box = go(world, "I paid", tool("check_payment"),
                  answer("We have not received the payment yet. Once it goes through, your booking is confirmed "
                         "automatically; no need to pay again."))
    assert res.calls[0]["result"]["facts"]["payment_received"] is False and res.sent == ["ai"]


def test_talk_to_owner_sends_the_guest_nothing_and_alerts_the_team_with_the_note(world):
    tools = AgentTools(world.bot, GUEST, Outbox(GUEST, dry_run=True))
    out = tools.talk_to_owner({"reason": "other", "staff_note": "don't call my father"})
    assert out["sent"] == ["staff:alert"] and out["facts"]["numbers"]
    alert = tools.out.queue[0]
    assert alert.kind == "staff" and "don't call my father" in alert.text and STAFF in alert.to


def test_a_room_question_answered_with_send_photos_never_switches_the_room(world):
    go(world, ASK, tool("update_enquiry", **STAY), answer(ASK_CONTACT), live=True)
    before = Enquiry.load(world.repo, GUEST).room_picks
    res, _ = go(world, "do you have a family suite also?", tool("send_photos", kind="room_type", room_type="Family Suite"),
                answer("Yes, the Family Suite is also free."), live=True)
    assert [c["name"] for c in res.calls] == ["send_photos", "reply"]
    assert Enquiry.load(world.repo, GUEST).room_picks == before


# --------------------------------------------------------------------------- 4. the prompt


def test_the_prompt_is_short_with_the_owners_words_and_five_examples():
    assert len(SYSTEM.splitlines()) < 60
    for words in ("Hi, How may I assist you?", "Please share these details:", "To check the price, please share",
                  "Please share your name and mobile number to proceed.", "Sorry, we have properties only in {towns}.",
                  "Please contact the team directly: {numbers}", "For more detailed discussion, kindly call: {numbers}",
                  "Thank you. Please let me know if you need any help with your booking.",
                  "We have not received the payment yet."):
        assert words in SYSTEM
    assert all(f"\n{n}. " in SYSTEM for n in range(1, 6)) and "reply()" in SYSTEM


# --------------------------------------------------------------------------- 5. the old reminder in agent mode


def test_agent_mode_sweep_skips_only_the_still_open_reminder(world):
    w = world
    w.bot.config = w.bot.config.model_copy(update={"agent_mode": "agent"})
    book_live(w)
    hold = next(b for b in w.repo.list_bookings(phone=GUEST, status="hold", limit=5))
    w.clock.advance(seconds=(hold.hold_expires_at - w.clock.now()).total_seconds() - 120)  # 2 minutes left
    before = len(w.wa.texts(GUEST))
    out = web.sweep(services=SimpleNamespace(bot=w.bot))
    assert out["reminded"] == 0 and len(w.wa.texts(GUEST)) == before
    w.clock.advance(minutes=5)
    assert web.sweep(services=SimpleNamespace(bot=w.bot))["expired"] >= 1  # the hold is still released


def test_old_mode_sweep_still_sends_the_reminder(world):
    w = world
    book_live(w)
    hold = next(b for b in w.repo.list_bookings(phone=GUEST, status="hold", limit=5))
    w.clock.advance(seconds=(hold.hold_expires_at - w.clock.now()).total_seconds() - 120)
    assert web.sweep(services=SimpleNamespace(bot=w.bot))["reminded"] == 1


# --------------------------------------------------------------------------- 6. outbox photo order (kept)


def test_a_text_after_photos_waits_and_every_send_returns_first():
    log = []
    box = Outbox("+91", lambda to, x: log.append(("text", x)), lambda to, refs: log.append(("photos", tuple(refs))),
                 photo_gap_s=1.0, sleep=lambda s: log.append(("wait", s)))
    box.text("room_label", "*Room 101 - Deluxe*")
    box.photos("photos:2", ["a", "b"])
    box.text("ai", ASK_CONTACT)
    box.flush()
    assert log == [("text", "*Room 101 - Deluxe*"), ("photos", ("a", "b")), ("wait", 1.0), ("text", ASK_CONTACT)]


def test_the_wait_is_one_second_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_PHOTO_GAP_S", raising=False)
    assert photo_gap() == 1.0
    monkeypatch.setenv("AGENT_PHOTO_GAP_S", "0")
    assert photo_gap() == 0.0
