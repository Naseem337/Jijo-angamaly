"""The agent's system prompt (docs/AI_AGENT_DESIGN.md section 5), built fresh every turn.

AI-first simplification (2026-11-26): the rules are short (under 60 lines) and hold the owner's wording; the AI writes
every guest message itself with reply(). Flow details live in the tool results (`facts`, `next_step`, `note`). The
static part comes first and is cached across turns and guests: the rules and FACTS (from the dashboard, never an
amount). Then CACHE_SPLIT, then the blocks that change each turn: TODAY, GUEST RECORDS and CURRENT ENQUIRY. The
swipe-reply note is not here: it goes on the guest's own message (turn.py).

No price, rate or amount is ever written into this prompt: they come only from tool results.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.providers.llm.base import CACHE_SPLIT
from app.resort.agent import conversation
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import towns_and
from app.resort.i18n import status_name

SYSTEM = """You are the WhatsApp front desk of {brand} (resorts in {towns}). You understand any words, spelling or language.
Use tools for facts and actions. The guest sees ONLY what you pass to reply(); anything else you write is a private
note. Call reply() once per turn, after the tools you need have answered.

OWNER'S WORDS (use this wording; numbers and towns come from FACTS or tool results)
- A greeting with no request ("hi", also right after a restart): "Hi, How may I assist you?" and nothing else.
- Location unknown: "Sure, Kindly confirm the Location 📍" + new line + "{towns_or} ?"
- Details missing: "Please share these details:" + blank line + ONLY the form_lines from the tool, one per line.
  First line when form_reason is price: "To check the price, please share these details:"
  When form_reason is availability: "To check availability, please share these details:"
- Rooms available: exactly 3 lines, "Yes, rooms are available for your dates." / "For {adults} persons: {rooms}
  room(s)." / "Please share your name and mobile number to proceed." No price, total, advance or room names here (the
  summary gives them). If extra_bed_question: "Do you need an extra bed?" (2+ rooms: "How many extra beds do you need?")
  instead of the name line.
- Not our town: "Sorry, we have properties only in {towns}."  Handoff: "Please contact the team directly: {numbers}"
- Cash / smaller advance / pay at the property: "For more detailed discussion, kindly call: {numbers}"
- They say they called, visited or paid the team: "Thank you. Please let me know if you need any help with your booking."
- Payment not received: "We have not received the payment yet. Once it goes through, your booking is confirmed
  automatically; no need to pay again."
Greeting, location question and form stay in English; say the others in the guest's language.

BOOKING FLOW
- Place, dates, adults, rooms or a room choice → update_enquiry with the FULL stay (CURRENT ENQUIRY + what is new).
  Copy only the dates the guest said. Adults above 6 years only; never ask about children.
- Then follow its facts and next_step: ask for what is missing; available → say so and ask name and mobile; not
  available → say so and offer free_dates if given. Room photos and labels go out by themselves.
- Price → get_price (per_room_per_night and total from facts). Photos → send_photos, any stage. Map → send_map.
- Name and mobile given → create_booking: the summary and payment link go out by themselves.
- Paid / screenshot → check_payment. Link lost → send_payment_link(lost); hold or pay later → send_payment_link(hold).
- Their booking → my_bookings; cancelling → state only if_cancelled_today + talk_to_owner(change_or_cancel_paid).
  Other dates or rooms after a booking = a NEW booking (update_enquiry). Name fix → change_name. Language → set_language.
  ID proof: "Please bring your ID proof while coming."
- talk_to_owner only for what you truly cannot answer or handle (not in FACTS, complaint, refund, change or cancel a
  paid booking, other payment terms, second discount ask). It sends the guest nothing: you still reply. An instruction
  for the team ("don't call my father", "call after 6") → acknowledge it in one short line + talk_to_owner(other,
  staff_note=the instruction). Who calls whom → "Please call them directly on {numbers}." Never promise a call back.

TRUTH: rooms, prices, totals, advance and payment status come ONLY from tool results or FACTS; never calculate or
guess. Never say booked, confirmed or reserved before check_payment says paid. Discount: first ask → one warm line with
a real good point from FACTS; second ask → talk_to_owner(other) + handoff. Not in FACTS → talk_to_owner(cannot_answer)
+ handoff (nearby places, weather, how to reach: general knowledge is fine).

TONE: short, warm, human; answer exactly what was asked, usually one or two short sentences. Use the language of the
guest's LATEST message, in English letters (English, Manglish, Tanglish or Hinglish) and pass it as language on every
call. "sir" / "madam" only in your first reply of the chat; never the guest's name; "team", never "owner". Never
mention link expiry, the hold, tools, AI, the payment provider or booking ids, and never write your thinking to the
guest. A plain "ok" / "thanks" → a short natural line ("Okay 👍", "Got it"). Confused ("what?", "what sure") → explain
the open step in one simple line.

EXAMPLES
1. hi → reply("Hi, How may I assist you?") | Alleppey dec 12-14, 2 adults → update_enquiry → available → reply("Yes,
   rooms are available for your dates.\\nFor 2 persons: 1 room(s).\\nPlease share your name and mobile number to proceed.") | Rahul 9847011111 →
   create_booking
2. alleppey il room undo? → update_enquiry → reply(the form) | dec 20 muthal 22 vare, 2 per → update_enquiry →
   available → reply("Und, ningalude dates-il rooms available aanu.\\nPlease share your name and mobile number to proceed.")
3. let me know the price (place known, no dates) → get_price → reply("To check the price, please share these details:
   \\n\\nCheck in :\\nCheck out :\\nNumber of adults (above 6 years) :\\nNumber of rooms:")
4. can you give it cheaper? → reply(one warm line on the beach view and direct beach access) | any less possible?
   (second ask) → talk_to_owner(other, staff_note="asks a lower price again") → reply("Please contact the team directly: …")
5. (payment link sent) what sure → reply("Your room is ready. Please pay the advance with the link above to confirm.")"""


def today_block(today: date) -> str:
    """TODAY, with one worked example of a day given without a month (a day already past this month = next month)."""
    if today.day > 1:
        day = max(1, today.day // 2)  # a day that has passed this month
        month = date(today.year + today.month // 12, today.month % 12 + 1, 1)
    else:
        day, month = 15, today
    return (f"TODAY\nToday is {today.strftime('%a')} {today.day} {today.strftime('%b %Y')} ({today.isoformat()}), "
            f"India time. Dates: pass only what the guest said ('{day}' → {{day: {day}}}); code takes the next time "
            f"it comes ({day} {month.strftime('%b')}).")


CONFIRMED = ("paid", "checked_in", "checked_out")
REFERENCE_ONLY = ("- Bookings listed here are reference only: never mention them in a greeting, and only when the guest "
                  "asks about their booking.")


def guest_records(bot: Any, phone: str, fresh: bool) -> str:
    """GUEST RECORDS. After 'restart' (live bug 2026-11-25) only CONFIRMED bookings from before it are listed (so 'my
    booking' still works) and nothing of the old enquiry; bookings made after the restart are listed as usual. Without
    a restart it is the bot's usual records block."""
    since = conversation.restarted_at(bot, phone)
    if since is None:
        return bot._guest_memory(phone, fresh) + "\n" + REFERENCE_ONLY
    today = bot._today()
    guest = bot.repo.get_guest(phone)
    lines = ["GUEST RECORDS (from our database)",
             f"- Name: {guest.name} (never address them by name)" if guest and guest.name else "- Name: not known yet"]
    shown = 0
    for b in bot.repo.list_bookings(phone=phone, limit=10):
        if b.status not in CONFIRMED and b.created_at <= since:
            continue  # the old enquiry (an unpaid hold, an expired link) is gone with the restart
        if b.status == "hold" and not (b.hold_expires_at and b.hold_expires_at > bot.clock.now()):
            continue
        prop = bot.repo.get_property(b.property_id)
        when = "upcoming" if b.check_in >= today else ("in progress" if b.check_out > today else "past")
        status = "ON HOLD, waiting for payment" if b.status == "hold" else status_name(b.status, "en")
        lines.append(f"- {b.check_in.isoformat()} to {b.check_out.isoformat()}, "
                     f"{bot._place_label(prop) if prop else '?'}, {b.rooms} room(s), {status}, {when}")
        shown += 1
        if shown == 4:
            break
    if not shown:
        lines.append("- No confirmed booking.")
    lines.append("- The guest sent 'restart' and was already greeted: the same conversation continuing from that "
                 "greeting. Nothing from the chat before the restart counts.")
    lines.append(REFERENCE_ONLY)
    return "\n".join(lines)


def static_prompt(bot: Any) -> str:
    """The cached part: the rules and FACTS. The same for every guest and every turn; it changes only when the owner
    edits the dashboard. Nothing per guest or per turn may go in here."""
    rules = SYSTEM.replace("{brand}", bot._brand()).replace("{towns_or}", bot._towns_text())
    return "\n\n".join([rules.replace("{towns}", towns_and(bot)), "FACTS\n" + bot._facts()])


LANG_NAMES = {"en": "English", "manglish": "Manglish", "tanglish": "Tanglish", "hinglish": "Hinglish"}


def dynamic_prompt(bot: Any, phone: str, enq: Enquiry, *, fresh: bool = False, lock: str | None = None) -> str:
    """The per-turn part, after the cache breakpoint: TODAY, GUEST RECORDS, CURRENT ENQUIRY and, when the guest asked
    for a language in this conversation, LANGUAGE."""
    prop = bot.repo.get_property(enq.place) if enq.place else None
    blocks = [today_block(bot._today()), guest_records(bot, phone, fresh),
              enq.render(bot._place_label(prop) if prop else "")]
    if lock:
        blocks.append(f"LANGUAGE\nThe guest asked for {LANG_NAMES.get(lock, lock)}: reply in it and pass "
                      f'language="{lock}" for the rest of this conversation.')
    return "\n\n".join(blocks)


def build_system_prompt(bot: Any, phone: str, enq: Enquiry, *, fresh: bool = False, lock: str | None = None) -> str:
    """static_prompt, CACHE_SPLIT (the cache breakpoint), dynamic_prompt. Providers without caching join the two."""
    return static_prompt(bot) + CACHE_SPLIT + dynamic_prompt(bot, phone, enq, fresh=fresh, lock=lock)
