"""No price reaches the AI except through a tool (owner, 2026-11-24): the FACTS carry no amounts, get_price reads the
dashboard database at call time, and a reply with a rupee amount no tool gave in that turn is blocked."""

from __future__ import annotations

import re

from test_bot import GUEST, last_tool, reply, say, tool, world  # noqa: F401  (world is a fixture)

from app.resort.chat import _Turn

AMOUNT = re.compile(r"₹|\brs\.?\s?\d|\brupees?\b|\d{1,3},\d{3}|\b\d{4,}\b|\d\s?%", re.IGNORECASE)
PHONES = re.compile(r"\+?\d[\d ]{8,}\d|https?://\S+")  # the owner's numbers and map link are facts, not amounts


def no_amounts(text: str) -> None:
    text = PHONES.sub("", text)
    assert not AMOUNT.search(text), AMOUNT.search(text).group(0)


def checked_stay(w) -> None:
    """A guest whose stay (Alleppey, 10-12 Nov, 2 persons) is checked: Deluxe is offered at ₹4,000 a night."""
    say(w, "Alleppey, 10 nov to 12 nov, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", check_out="2026-11-12", adults=2),
        reply("[no reply]"))


def get_price(w) -> dict:
    ctx = _Turn(GUEST, w.repo.get_state(GUEST), text="what will it come to")
    out = w.bot._tool_get_price(ctx, {})
    assert ctx.priced
    return out


# --------------------------------------------------------------------------- 1. get_price reads the dashboard now


def test_get_price_reads_the_dashboard_database_and_a_changed_rate_shows_at_once(world):
    w = world
    checked_stay(w)
    out = get_price(w)
    assert out["room_type"] == "Deluxe" and out["total"] == 8000 and out["per_room_per_night"] == 4000
    assert "next_step" not in out  # only a price answer: nothing is booked or asked

    w.deluxe.base_rate = 5000  # the owner changes the rate in the dashboard
    w.repo.save_room_type(w.deluxe)
    out = get_price(w)
    assert out["total"] == 10000 and out["per_room_per_night"] == 5000


def test_get_price_for_a_named_room_is_only_a_price_never_a_switch(world):
    w = world
    checked_stay(w)
    before = w.repo.get_state(GUEST)["bk"].get("chosen")
    ctx = _Turn(GUEST, w.repo.get_state(GUEST), text="price of the family suite")
    out = w.bot._tool_get_price(ctx, {"room_type": "Family Suite"})
    assert out["room_type"] == "Family Suite" and out["total"] == 16000
    assert ctx.st["bk"].get("chosen") == before  # the enquiry is unchanged


def test_get_price_with_no_stay_known_sends_the_ai_to_ask_for_the_details(world):
    w = world
    out = w.bot._tool_get_price(_Turn(GUEST, w.repo.get_state(GUEST)), {})
    assert "error" in out and "ask_stay_details" in out["error"]


def test_get_price_is_a_tool_the_ai_is_offered(world):
    w = world
    say(w, "do you have a pool?", GUEST, reply("Sorry, I am not sure."))
    assert "get_price" in {x.name for x in w.llm.calls[-1].tools}


# --------------------------------------------------------------------------- 2. the FACTS carry no amounts


def test_the_facts_hold_no_rupee_amount_advance_percent_or_rate(world):
    w = world
    p = w.p1
    p.description = "A beach resort. Rooms from ₹3,000 a night. Calm and green."
    p.highlights = ["Beach view from every room", "Breakfast included at ₹250 per head"]
    p.meal_plan = "Breakfast only"
    p.early_checkin_time, p.early_checkin_rate = "10:00", 1500
    p.policies = "Free cancellation 7 days before. Late check-out costs Rs 800."
    w.repo.save_property(p)
    w.deluxe.description = "Sea view, AC. Extra pillow ₹100."
    w.repo.save_room_type(w.deluxe)

    facts = w.bot._facts()
    no_amounts(facts)
    for rate in ("4000", "4,000", "8000", "8,000", "500", "1500", "1,500", "50%"):
        assert rate not in facts
    # the real facts are still there
    assert "Beach view from every room" in facts and "Calm and green." in facts and "Free cancellation 7 days before." in facts
    assert "Extra bed: available" in facts and "Early check-in" in facts and "9497861924" in facts
    assert "Sea view, AC." in facts


def test_the_whole_system_prompt_gives_no_rate_to_the_ai(world):
    w = world
    say(w, "do you have a pool?", GUEST, reply("Sorry, I am not sure."))
    facts = w.llm.calls[0].system.rsplit("\nFACTS\n", 1)[1].split("GUEST RECORDS")[0]
    assert "PLACE 'Pozhiyoram, Alleppey'" in facts
    no_amounts(facts)


# --------------------------------------------------------------------------- 3. the amount guard


def test_an_amount_no_tool_gave_this_turn_is_blocked_and_the_ai_fixes_it(world):
    w = world
    checked_stay(w)
    sent = len(w.wa.texts())
    # first reply invents a rate; the rewrite (asked once) leaves the amount out
    say(w, "hmm what will it come to for us", GUEST,
        reply("It is about ₹2,500 a night."), reply("Please tell me your name and mobile number."))
    assert w.llm.calls  # the AI answered (no regex shortcut took the message)
    new = " ".join(w.wa.texts()[sent:])
    assert "2,500" not in new and "2500" not in new
    rewrite_ask = w.llm.calls[-1].messages[-1].content
    assert "2,500" in rewrite_ask and "No tool gave you them" in rewrite_ask


def test_an_amount_still_in_the_rewrite_is_dropped_never_sent(world):
    w = world
    checked_stay(w)
    sent = len(w.wa.texts())
    say(w, "hmm what will it come to for us", GUEST,
        reply("It is about ₹2,500 a night."), reply("Sure, ₹2,500 only. Rooms are available."))
    new = " ".join(w.wa.texts()[sent:])
    assert "2,500" not in new


def test_an_amount_get_price_gave_this_turn_is_allowed(world):
    w = world
    checked_stay(w)
    # through the real loop: the AI calls get_price, then states the amounts it was given
    say(w, "hmm what will it come to for us", GUEST,
        tool("get_price"), reply("The total is ₹8,000 for 2 nights, ₹4,000 a night."))
    res = last_tool(w)
    assert res["total"] == 8000 and res["per_room_per_night"] == 4000
    # the guard itself: the amounts get_price returned are backed, an invented one is not
    ctx = _Turn(GUEST, w.repo.get_state(GUEST), text="what will it come to")
    w.bot._run_tool(ctx, type("C", (), {"name": "get_price", "arguments": {}, "arguments_error": None, "id": "1"})())
    ok = "The total is ₹8,000 for 2 nights, ₹4,000 a night."
    assert w.bot._unbacked_amounts(ok, ctx, "what will it come to") == []
    assert w.bot._tool_prices_only(ctx, ok) == ok
    bad = "The total is ₹8,000 and the advance is ₹3,333."
    assert w.bot._unbacked_amounts(bad, ctx, "what will it come to") == [3333]
    assert w.bot._tool_prices_only(ctx, bad) == ""  # the sentence with the unbacked amount is dropped


def test_fix_amounts_asks_the_ai_once_and_keeps_a_clean_rewrite(world):
    w = world
    ctx = _Turn(GUEST, w.repo.get_state(GUEST), text="how is it")
    w.llm.script.append(reply("Please tell me your dates."))
    fixed = w.bot._fix_amounts(w.llm, "system", [], "It is about ₹2,500 a night.", "how is it", ctx)
    assert fixed == "Please tell me your dates."
    ask = w.llm.calls[-1].messages[-1].content
    assert "₹2,500" in ask and "No tool gave you them" in ask
    # a rewrite that still states an unbacked amount is not used
    w.llm.script.append(reply("Sure, ₹2,500 only."))
    original = "It is about ₹2,500 a night."
    assert w.bot._fix_amounts(w.llm, "system", [], original, "how is it", ctx) == original
    # nothing to fix: no AI call
    calls = len(w.llm.calls)
    assert w.bot._fix_amounts(w.llm, "system", [], "Yes, rooms are free.", "how is it", ctx) == "Yes, rooms are free."
    assert len(w.llm.calls) == calls


def test_an_amount_the_guest_wrote_may_be_repeated_but_another_one_may_not(world):
    w = world
    ctx = _Turn(GUEST, w.repo.get_state(GUEST), text="under 5000 only")
    assert w.bot._unbacked_amounts("Yes, ₹5,000 is fine.", ctx, "under 5000 only") == []
    assert w.bot._unbacked_amounts("Yes, ₹6,500 is fine.", ctx, "under 5000 only") == [6500]
    ctx.numbers.add(6500)  # a tool gave it this turn
    assert w.bot._unbacked_amounts("Yes, ₹6,500 is fine.", ctx, "under 5000 only") == []
