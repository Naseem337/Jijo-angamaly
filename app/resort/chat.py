"""The guest conversation: plain free text in any language, answered like a person at the front desk.

No menus, lists or buttons. The AI reads the guest's message, replies in the guest's own language and
style (English, Malayalam, Manglish, Hindi/Hinglish, Tamil/Tanglish) and asks for what is missing: place,
check-in date, nights, persons, rooms. It never decides a fact: prices, capacity, availability and the
booking all come from the tools below, which run the same code the dashboard uses.

Per-guest state in repo.get_state(phone):
    st["bk"] = {property_id, check_in, check_out, adults, children,
                chosen: {room_type_id, rooms, extra_beds, total, advance, turn}}
    st["turn"] = counts the guest's messages; a booking can only be created on a LATER turn than the
                 one where the summary was shown, so the guest's "yes" is always a separate message.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.core.clock import to_ist
from app.providers.llm import ChatMessage, LLMError, ToolCall, ToolSpec
from app.resort import availability as av
from app.resort import leads
from app.resort.db import SoldOut
from app.resort.i18n import status_name, t
from app.resort.models import Booking, Property, RoomType
from app.resort.payments import PaymentError

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
MAX_REPLY_TOKENS = 700
HISTORY = 16
GREETING = re.compile(r"^\W*((hi+|hello+|hey+|hai+|hlo|hola|namaste|namaskar\w*|vanakkam|good\s*(morning|afternoon|evening))"
                      r"|നമസ്കാരം|ഹായ്|ഹലോ|வணக்கம்|ஹாய்|நமஸ்தே|नमस्ते|हाय|हेलो|हैलो)\W*$", re.IGNORECASE)
# Styles the AI may report. Fixed messages (payment, ID) use the script language; Latin-script styles get English.
STYLES = ("en", "ml", "manglish", "hi", "hinglish", "ta", "tanglish")
STYLE_LANG = {"en": "en", "ml": "ml", "hi": "hi", "ta": "ta", "manglish": "mn", "hinglish": "en", "tanglish": "en"}
INDIC = re.compile(r"[\u0900-\u097f\u0b80-\u0bff\u0d00-\u0d7f]")
HELLO = re.compile(r"^\W*(hello+|hey+)", re.IGNORECASE)
ACK_WORDS = frozenset("ok okay okk okey k kk kay thanks thank thankyou thx ty you fine cool noted got it sure alright "
                      "right yes ya yep sheri sari shari shaari athe aah aha da bro sir madam ശരി ഓക്കെ ഓകെ താങ്ക്സ് നന്ദി "
                      "ठीक है धन्यवाद शुक्रिया சரி நன்றி".split())
NO_REPLY = "[no reply]"


def is_ack(text: str) -> bool:
    """Only an acknowledgement: 'ok', 'okay thanks', 'sheri', '👍'. Nothing to answer."""
    s = text.strip().lower()
    if not s:
        return False
    if not re.search(r"\w", s):  # emoji or punctuation only
        return True
    words = [w.strip(".,!?;:()-'\"") for w in s.split()]  # not \w: it would cut Malayalam vowel signs
    return 0 < len(words) <= 4 and all(w in ACK_WORDS for w in words)


def display_phone(e164: str) -> str:
    """+919497861924 -> 9497861924 (how Indians write it); other countries keep the +."""
    return e164[3:] if e164.startswith("+91") and len(e164) == 13 else e164


def _money(n: int) -> str:
    return f"₹{n:,}"


@dataclass
class _Turn:
    """What one guest message did: tools may queue the payment link or take over the chat."""

    phone: str
    st: dict[str, Any]
    pay_message: str = ""
    handed_off: bool = False


LANGUAGE_FIELD = {"type": "string", "enum": list(STYLES),
                  "description": "the language style of the guest's latest message (Manglish = Malayalam in English letters)"}

TOOLS = [
    ToolSpec(
        "check_availability",
        "Check which rooms are free for the guest's stay and what they cost. Call it as soon as you know the place, "
        "the check-in date, the number of nights (or check-out date) and the number of guests. Give the rooms "
        "count too when the guest has said it.",
        {"type": "object", "properties": {
            "place": {"type": "string", "description": "the place the guest wants, e.g. Alleppey or Munnar"},
            "check_in": {"type": "string", "description": "YYYY-MM-DD"},
            "check_out": {"type": "string", "description": "YYYY-MM-DD (or give nights instead)"},
            "nights": {"type": "integer"},
            "adults": {"type": "integer", "description": "total persons if the guest did not split adults and children"},
            "children": {"type": "integer", "description": "only when the guest mentioned children"},
            "rooms": {"type": "integer", "description": "rooms the guest asked for, if any"},
            "language": LANGUAGE_FIELD},
         "required": ["place", "check_in", "adults"]}),
    ToolSpec(
        "prepare_booking",
        "After the guest has chosen a room type (and how many rooms), lock the figures and get the exact summary "
        "numbers. Then show the guest the full summary and ask them to confirm. Does not book anything.",
        {"type": "object", "properties": {
            "room_type": {"type": "string", "description": "room type name as shown by check_availability"},
            "rooms": {"type": "integer"},
            "extra_beds": {"type": "integer", "description": "extra beds the guest asked for (default 0)"},
            "language": LANGUAGE_FIELD},
         "required": ["room_type", "rooms"]}),
    ToolSpec(
        "create_booking",
        "Create the booking and the payment link. Only after the guest has seen the summary from prepare_booking "
        "and replied that they confirm. The payment link is sent automatically right after your reply.",
        {"type": "object", "properties": {
            "guest_name": {"type": "string", "description": "name for the booking, if not already known"},
            "from_city": {"type": "string", "description": "the city they are coming from, only if they said it"},
            "language": LANGUAGE_FIELD}}),
    ToolSpec(
        "send_payment_link",
        "Send the guest their payment link again (they lost it, it did not open, or they ask for it). Only works "
        "while their booking is still on hold. The link is sent automatically after your reply.",
        {"type": "object", "properties": {"language": LANGUAGE_FIELD}}),
    ToolSpec(
        "my_bookings",
        "The guest's own recent bookings with status, amounts and the owner's contact. Use when they ask about "
        "an existing booking or payment.",
        {"type": "object", "properties": {}}),
]


class ChatMixin:
    # ------------------------------------------------------------------ the prompt

    def _places(self) -> list[Property]:
        return self.repo.list_properties()

    @staticmethod
    def _place_label(p: Property) -> str:
        return p.place or p.name

    def _rooms_facts(self, p: Property) -> list[str]:
        lines = []
        today = self._today()
        for rt in self.repo.list_room_types(p.id):
            line = (f"  ROOM TYPE '{rt.name}': {rt.units} room(s), max {rt.max_occupancy} guests per room "
                    f"({rt.max_adults} adults, {rt.max_children} children). Rate per room per night {_money(rt.base_rate)}")
            if rt.weekend_rate and rt.weekend_rate != rt.base_rate:
                line += f", Fri and Sat nights {_money(rt.weekend_rate)}"
            seasons = [r for r in self.repo.list_rate_rules(rt.id) if r.end >= today]
            if seasons:
                line += ". Special dates: " + "; ".join(
                    f"{av.fmt_range(r.start, r.end)} {_money(r.rate)}" for r in seasons)
            extras = " ".join(x for x in (rt.description, rt.amenities) if x and "PLACEHOLDER" not in x)
            lines.append(line + "." + (f" {extras}" if extras else ""))
        return lines

    def _facts(self) -> str:
        out = []
        for p in self._places():
            numbers = ", ".join(display_phone(n) for n in p.contact_numbers) or "not set"
            out.append(f"PLACE '{self._place_label(p)}' (property name {p.name})")
            if p.description and "PLACEHOLDER" not in p.description:
                out.append(f"  About: {p.description}")
            if p.highlights:
                out.append(f"  Good points: {'; '.join(p.highlights)}")
            if p.map_url:
                out.append(f"  Location (Google Maps link, send it when the guest asks where it is): {p.map_url}")
            out.append(f"  Check-in {p.checkin_time}, check-out {p.checkout_time}. To confirm a booking the guest pays "
                       f"{p.advance_pct}% online now; the rest is paid at the property.")
            if p.extra_bed_rate:
                out.append(f"  Extra bed: {_money(p.extra_bed_rate)} per bed per night.")
            if p.policies:
                out.append(f"  Policies: {p.policies}")
            out.append(f"  Owner's phone/WhatsApp numbers: {numbers}")
            out += self._rooms_facts(p)
        return "\n".join(out)

    def _system_prompt(self, st: dict[str, Any]) -> str:
        today = self._today()
        return (
            f"You are the WhatsApp booking assistant of {self._brand()}. You talk like a friendly, sensible person at "
            "the front desk, never like a form or a menu. " + self.config.speaking_style + "\n\n"
            f"Today is {today.isoformat()} ({today.strftime('%A')}), India time.\n\n"
            "LANGUAGE\n"
            "- Work out the guest's language from their message. Never ask which language they want.\n"
            "- Reply in the same one: English, Malayalam script, Manglish (Malayalam in English letters), Hindi, "
            "Hinglish, Tamil script, or Tanglish. If they switch, you switch. Short words like 'ok' or 'hi' keep "
            "the language so far.\n"
            "- Use ONE language per reply, the guest's own. Malayalam typed in English letters ('avde engane und', "
            "'room undo', 'evide', 'ethra aaya') is Manglish: answer in Manglish, in English letters only, NEVER in "
            "Malayalam script. Malayalam script gets Malayalam script. English gets English. Never mix scripts or "
            "languages in one reply; plain English words like room, booking, check-in, payment and price are fine "
            "inside Manglish. Example: guest 'Alleppey il room undo' -> 'Und bro, Alleppey il rooms und. Ethra "
            "aalkkaar und, ethra divasam?'\n"
            "- The booking summary in step 6 is the one exception: always write it in English, whatever the language "
            "of the chat, so both sides can check the figures.\n"
            "- Pass the language style in the `language` field when you call check_availability.\n\n"
            "HOW TO TALK\n"
            "- Short, natural messages, like a real person on WhatsApp. One or two sentences. Ask at most one or two "
            "things at a time. No lists of options, no 'How may I assist you today', no emoji piles (one is fine).\n"
            "- Say only what was asked or what is needed next. Do not paste the property description. Do not mention "
            "offers, discounts or 'special' deals. Just state the price.\n"
            "- Never repeat details the guest already gave, except in the final summary.\n"
            "- Greet back in kind: 'hi' gets a short 'Hi!' and a light question about what they are looking for.\n"
            "- Do not chase the guest. If their message needs no answer (just 'ok', 'thanks', a thumbs up, or a closing "
            "remark) and you asked nothing, send no reply at all: answer with exactly [no reply]. If they want more, they "
            "will write. It is fine for the guest to have the last message in the chat.\n"
            "- Messages that arrive one after another belong together. A short message (a place, a date, a number, "
            "'yes') answers your last question or continues what they said before: join them using the chat so far. "
            "If the guest wrote 'avde engane und' and then 'Alleppey', they are asking how Alleppey is: answer that, "
            "do not start over or ask the same thing again.\n\n"
            "BOOKING, in this order, skipping what the guest already told you\n"
            "1. Which place. If you have several places, ask which one (name them). If there is only one, don't ask.\n"
            "2. Check-in date, and how many nights or the check-out date. If the date has no year, use the next "
            "time it comes. Work from today's date.\n"
            "3. How many persons.\n"
            "4. How many rooms. Tell them how many rooms their group needs at least, and let them choose.\n"
            "5. Call check_availability. Then offer what is free in plain words: each room type with its price for "
            "THEIR dates (use the totals and rate breakdown from the tool, mention when the rate changes within the "
            "stay). With more than one room type, give each with its price and let the guest pick.\n"
            "6. When they choose, call prepare_booking. Then send a clear summary: place, room type, rooms, dates "
            "and nights, persons, rate per night breakdown, total, the advance to pay now, the balance to pay at "
            "the property, check-in and check-out time. Ask them to confirm. If you do not know their name yet, ask "
            "for it in the same message, and in the same line ask which city they are coming from (optional: "
            "never insist, pass from_city to create_booking only if they tell you).\n"
            "7. Only after they confirm in a later message, call create_booking. Then tell them briefly to "
            "complete the payment to confirm the booking; the payment link follows. Do not write the link or the "
            "advance amount yourself, it is sent automatically.\n"
            "- Before the payment is received, NEVER say the room or booking is booked, confirmed, reserved or done. "
            "Say it will be confirmed once the payment is complete. The confirmation message arrives on its own.\n"
            "- If the party does not fit or the dates are sold out, say so and suggest another date, fewer rooms "
            "or the other place.\n\n"
            "MONEY AND FACTS\n"
            "- Every price, date, room count and availability you state must come from the tool results or the "
            "FACTS below. Never calculate your own totals. Never invent amenities or policies. If you do not "
            "know something, say the owner will tell them and give the owner's number.\n"
            "- Extra bed: never add one on your own. When a room would hold a third person (for example 3 persons "
            "in 1 room), ask the guest whether they want an extra bed and tell them its price from the FACTS. "
            "Pass extra_beds to prepare_booking only if they say yes; the tool adds the cost.\n\n"
            "PAYMENT\n"
            "- The payment link opens a secure Razorpay page. The guest can pay by UPI (GPay, PhonePe, Paytm, any UPI "
            "app or ID), debit or credit card, net banking or a wallet. Only the advance is paid online; the balance "
            "is paid at the property. The rooms are held for a limited time while they pay.\n"
            "- If they say the link is lost, did not open or they want it again, call send_payment_link. If the hold "
            "ran out, offer to book again (check the dates again). Explain any payment doubt simply and patiently. "
            "A confirmation message arrives here automatically as soon as the payment goes through.\n"
            "- If money was deducted but they got no confirmation, tell them not to pay again, that it normally "
            "shows within a few minutes, and give the owner's number. Never promise a refund.\n\n"
            "CANCELLATION, CHANGES AND REFUNDS\n"
            "- You cannot cancel or change a booking or promise a refund. Politely ask them to call the owner and give "
            "the owner's number(s) of the place they booked.\n\n"
            "NEGOTIATION\n"
            "- If the guest asks for a lower price, be warm and confident: the price is fair for what they get "
            "(use the real good points of the place from the FACTS) and you cannot reduce it. Never offer a "
            "discount. If they push again, hold the price once more with a different reason.\n"
            "- Only if they still keep pushing after that, say the owner can talk to them directly and give the "
            "owner's number(s).\n\n"
            "FACTS\n" + self._facts())

    # ------------------------------------------------------------------ the conversation

    def _history(self, phone: str) -> list[ChatMessage]:
        merged: list[ChatMessage] = []
        for m in self.repo.list_chat(phone, limit=HISTORY):
            role = "user" if m.direction == "in" else "assistant"
            text = m.text[:700]
            if merged and merged[-1].role == role:
                merged[-1] = ChatMessage(role, merged[-1].content + "\n" + text)
            else:
                merged.append(ChatMessage(role, text))
        while merged and merged[0].role != "user":
            merged.pop(0)
        return merged

    def _greet(self, phone: str, st: dict[str, Any], text: str = "") -> str:
        """'hi' gets 'Hi!', 'hello' gets 'Hello!', then one light question."""
        lang = st.get("lang", "en")
        word = "Hello" if HELLO.match(text) and not text.lower().lstrip().startswith("hey") else "Hey" if text.lower().lstrip().startswith("hey") else "Hi"
        props = self._places()
        if len(props) == 1:
            self._text(phone, t("greet_one", lang, hi=word, place=self._place_label(props[0])))
            return "greeting"
        places = [f'"{self._place_label(p)}"' for p in props]
        self._text(phone, t("greet_ask", lang, hi=word, places=f" {t('or', lang)} ".join(places)))
        return "greeting"

    def _only_latin_fix(self, llm, system: str, messages: list[ChatMessage], reply: str, guest_text: str) -> str:
        """A guest writing in English letters (English or Manglish) must not get Malayalam, Tamil or Hindi script:
        one rewrite, and if it still has script it is sent as it is."""
        if INDIC.search(guest_text) or not INDIC.search(reply):
            return reply
        ask = ChatMessage.user("Rewrite that reply using only English letters (Manglish or English). Do not use "
                               "Malayalam, Tamil or Hindi script. Keep the meaning and the same short style.")
        try:
            fixed = llm.complete(system, [*messages, ChatMessage("assistant", reply), ask], None,
                                 MAX_REPLY_TOKENS).text.strip()
        except LLMError:
            return reply
        return fixed if fixed and not INDIC.search(fixed) else reply

    def _converse(self, phone: str, st: dict[str, Any], text: str, in_id: int = 0) -> str:
        if self.debounce_s and in_id:  # a quick second message may follow: wait, and let the newest one answer
            time.sleep(self.debounce_s)
            if self.repo.last_chat_id(phone, "in") != in_id:
                return "superseded"
        st["turn"] = st.get("turn", 0) + 1
        ctx = _Turn(phone, st)
        messages = self._history(phone)
        system = self._system_prompt(st)
        reply = ""
        try:
            if self._llm is None:
                raise LLMError("none", "no LLM configured")
            llm = self._llm()
            response = llm.complete(system, messages, TOOLS, MAX_REPLY_TOKENS)
            for _ in range(MAX_TOOL_ROUNDS):
                if not response.tool_calls or ctx.handed_off:
                    break
                messages.append(response.to_message())
                for call in response.tool_calls:
                    content, is_error = self._run_tool(ctx, call)
                    messages.append(ChatMessage.tool_result(call.id, content, is_error=is_error))
                response = llm.complete(system, messages, TOOLS, MAX_REPLY_TOKENS)
            if not response.tool_calls:
                reply = self._only_latin_fix(llm, system, messages, response.text.strip(), text)
        except LLMError as exc:
            log.warning("AI answer failed: %s", exc)
        except Exception as exc:  # a missing key or a bad tool must never leave the guest unanswered
            log.exception("AI unavailable: %s", exc)
        self._save(phone, st)
        if ctx.handed_off:
            return "handoff"
        if NO_REPLY in reply.lower():
            return "quiet"
        if not reply:
            self._text(phone, t("ai_down", st.get("lang", "en")))
            self.alert_staff(f"Bot could not answer a message from {phone}: {text[:200]}", st.get("bk", {}).get("property_id"))
            return "ai_down"
        self._text(phone, reply.replace("—", "-").replace("**", "*"))
        if ctx.pay_message:
            self._text(phone, ctx.pay_message)
            return "payment_link"
        return "answered"

    # ------------------------------------------------------------------ tools

    def _run_tool(self, ctx: _Turn, call: ToolCall) -> tuple[str, bool]:
        handlers = {"check_availability": self._tool_check, "prepare_booking": self._tool_prepare,
                    "create_booking": self._tool_create, "my_bookings": self._tool_bookings,
                    "send_payment_link": self._tool_resend}
        handler = handlers.get(call.name)
        if handler is None or call.arguments_error:
            return json.dumps({"error": call.arguments_error or f"unknown tool {call.name}"}), True
        try:
            result = handler(ctx, call.arguments)
        except Exception as exc:
            log.exception("Tool %s failed", call.name)
            return json.dumps({"error": f"internal error: {exc}"}), True
        return json.dumps(result, ensure_ascii=False), "error" in result

    @staticmethod
    def _int(args: dict[str, Any], key: str, default: int = 0) -> int:
        try:
            return int(args.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _find_property(self, place: str) -> Property | None:
        props = self._places()
        if len(props) == 1:
            return props[0]
        query = re.sub(r"[^a-z0-9 ]", " ", (place or "").lower())
        best, best_score = None, 0
        for p in props:
            words = set(re.findall(r"[a-z0-9]{4,}", f"{p.name} {p.city} {p.place}".lower()))
            score = sum(1 for w in words if w[:5] in query)
            if score > best_score:
                best, best_score = p, score
        return best

    def _fits(self, prop: Property, bk: dict[str, Any]) -> list[tuple[RoomType, list[int]]]:
        ci, co = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
        now = self.clock.now()
        out = []
        for rt in self.repo.list_room_types(prop.id):
            options = av.room_options(rt, bk["adults"], bk["children"], self.repo.free_rooms(rt, ci, co, now))
            if options:
                out.append((rt, options))
        return out

    @staticmethod
    def _set_style(st: dict[str, Any], a: dict[str, Any]) -> None:
        """The AI reports the guest's language style; the fixed messages (payment link, confirmations) follow it."""
        style = a.get("language")
        if style in STYLES:
            st["lang"], st["style"] = STYLE_LANG[style], style

    def _tool_check(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        st = ctx.st
        self._set_style(st, a)
        prop = self._find_property(a.get("place", ""))
        if prop is None:
            return {"error": "unknown place", "places": [self._place_label(p) for p in self._places()]}
        try:
            ci = date.fromisoformat(str(a.get("check_in", "")))
            nights = self._int(a, "nights")
            co = date.fromisoformat(str(a["check_out"])) if a.get("check_out") else ci + timedelta(days=nights)
        except (ValueError, KeyError):
            return {"error": "check_in and check_out must be YYYY-MM-DD, or give nights"}
        if problem := av.check_dates(ci, co, self._today()):
            return {"error": problem}
        adults, children = self._int(a, "adults"), self._int(a, "children")
        if adults < 1 or adults + children > av.MAX_GUESTS:
            return {"error": f"guests must be between 1 and {av.MAX_GUESTS}"}
        bk = {"property_id": prop.id, "check_in": ci.isoformat(), "check_out": co.isoformat(),
              "adults": adults, "children": children}
        st["bk"] = bk
        wanted = self._int(a, "rooms")
        leads.save_lead(self.repo, ctx.phone, self.clock.now(), property_id=prop.id, check_in=ci.isoformat(),
                        check_out=co.isoformat(), adults=adults, children=children, rooms=wanted, room_type="",
                        stage="asked", booking_id="")
        options = []
        for rt, rooms_ok in self._fits(prop, bk):
            rooms = wanted if wanted in rooms_ok else rooms_ok[0]
            q = av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, rooms)
            options.append({"room_type": rt.name, "rooms_possible": rooms_ok, "quoted_for_rooms": rooms,
                            "rate_per_room_per_night": av.rate_summary(q.per_night), "total_for_stay": q.total,
                            "advance_now": q.advance, "balance_at_property": q.total - q.advance})
        out: dict[str, Any] = {"place": self._place_label(prop), "check_in": ci.isoformat(),
                               "check_out": co.isoformat(), "nights": (co - ci).days, "adults": adults,
                               "children": children, "options": options}
        if not options:
            out["available"] = False
            out["reason"] = "no room type is free for this many guests on these dates (sold out or too many guests)"
        elif wanted and not any(wanted in o["rooms_possible"] for o in options):
            out["note"] = (f"{wanted} room(s) is not possible for {adults + children} guest(s); "
                           "rooms_possible lists the counts that work")
        return out

    def _tool_prepare(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        st = ctx.st
        self._set_style(st, a)
        bk = st.get("bk")
        if not bk:
            return {"error": "call check_availability first"}
        prop = self.repo.get_property(bk["property_id"])
        wanted = str(a.get("room_type", "")).lower().strip()
        fits = self._fits(prop, bk)
        chosen = next(((rt, o) for rt, o in fits if rt.name.lower() == wanted or rt.id.lower() == wanted), None) or next(
            ((rt, o) for rt, o in fits if wanted and (wanted in rt.name.lower() or rt.name.lower() in wanted)), None)
        if chosen is None and len(fits) == 1:
            chosen = fits[0]
        if chosen is None:
            return {"error": "that room type is not free any more or not offered",
                    "free_room_types": [rt.name for rt, _ in fits]}
        rt, options = chosen
        rooms, beds = self._int(a, "rooms", options[0]), self._int(a, "extra_beds")
        if rooms not in options:
            return {"error": f"{rooms} room(s) is not possible; choose one of {options}"}
        if beds and (not prop.extra_bed_rate or beds > rooms):
            return {"error": "extra beds are not available" if not prop.extra_bed_rate
                    else f"at most one extra bed per room ({rooms})"}
        ci, co = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
        q = av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, rooms, beds)
        bk["chosen"] = {"room_type_id": rt.id, "rooms": rooms, "extra_beds": beds, "total": q.total,
                        "advance": q.advance, "turn": st["turn"]}
        leads.save_lead(self.repo, ctx.phone, self.clock.now(), room_type=rt.name, rooms=rooms, stage="summary")
        guest = self.repo.get_guest(ctx.phone)
        out = {"place": self._place_label(prop), "room_type": rt.name, "rooms": rooms, "extra_beds": beds,
               "check_in": f"{ci.isoformat()} ({ci.strftime('%A')})", "check_out": f"{co.isoformat()} ({co.strftime('%A')})",
               "nights": q.nights, "adults": bk["adults"], "children": bk["children"],
               "rate_per_room_per_night": av.rate_summary(q.per_night), "total": q.total, "advance_now": q.advance,
               "balance_at_property": q.total - q.advance, "check_in_time": prop.checkin_time,
               "check_out_time": prop.checkout_time, "guest_name_known": bool(guest and guest.name),
               "next_step": "show this summary and ask the guest to confirm"}
        if beds:
            out["extra_bed_cost"] = beds * prop.extra_bed_rate * q.nights
        return out

    def _tool_create(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        st, phone = ctx.st, ctx.phone
        self._set_style(st, a)
        bk = st.get("bk") or {}
        chosen = bk.get("chosen")
        if not chosen:
            return {"error": "no prepared booking: call check_availability and prepare_booking first"}
        if chosen["turn"] >= st["turn"]:
            return {"error": "the guest has not seen the summary yet. Show it and wait for their confirmation message"}
        now = self.clock.now()
        guest = self.repo.touch_guest(phone, now, language=st.get("lang"))
        name = (a.get("guest_name") or "").strip()[:60] or guest.name
        if not name:
            return {"error": "ask the guest's name for the booking"}
        if name != guest.name:
            guest.name = name
            self.repo.upsert_guest(guest)
        prop = self.repo.get_property(bk["property_id"])
        rt = self.repo.get_room_type(chosen["room_type_id"])
        ci, co = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
        q = av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, chosen["rooms"], chosen["extra_beds"])
        if q.total != chosen["total"]:  # a rate changed since the summary: the guest must see the new total
            bk.pop("chosen", None)
            return {"error": "the price changed since the summary; call prepare_booking again and re-confirm"}
        lang = st.get("lang", "en")
        hold = timedelta(minutes=self.config.hold_minutes)
        booking = Booking(prop.id, rt.id, phone, ci, co, bk["adults"], bk["children"], chosen["rooms"], q.total,
                          q.advance, now, guest_name=name, language=lang, hold_expires_at=now + hold,
                          notes=f"Extra beds: {chosen['extra_beds']}" if chosen["extra_beds"] else "")
        try:
            self.repo.create_hold(booking, now)
        except SoldOut:
            bk.pop("chosen", None)
            return {"error": "those rooms were just taken by someone else; call check_availability again"}
        try:  # Razorpay wants a link that lives at least 15 minutes
            link = self.payments.create_payment_link(
                booking.advance, booking.id, name, phone,
                f"{prop.name}: {rt.name} x {booking.rooms}, {av.fmt_range(ci, co)}",
                int((now + max(hold, timedelta(minutes=16))).timestamp()))
        except PaymentError as exc:
            log.error("Payment link failed for %s: %s", booking.id, exc)
            booking.status, booking.notes = "failed", f"payment link failed: {exc}"
            self.repo.save_booking(booking)
            self._text(phone, t("pay_failed_link", lang))
            self.handoff(phone, st, f"payment link could not be created for {booking.id}", notify_guest=False)
            ctx.handed_off = True
            return {"error": "payment link failed; the team has been told and will contact the guest"}
        booking.payment_link_id, booking.payment_link_url = link.id, link.url
        self.repo.save_booking(booking)
        st["booking_id"] = booking.id
        st.pop("bk", None)
        leads.save_lead(self.repo, phone, now, stage="held", booking_id=booking.id,
                        from_city=(a.get("from_city") or "").strip()[:60] or None)
        ctx.pay_message = t("pay", lang, id=booking.id, minutes=self.config.hold_minutes,
                            advance=f"{booking.advance:,}", url=link.url)
        return {"ok": True, "booking_id": booking.id, "rooms_held_minutes": self.config.hold_minutes,
                "note": "the payment link and amount are sent automatically after your reply; do not write them. "
                         "The booking is NOT confirmed yet: tell the guest to complete the payment to confirm it"}

    def _tool_bookings(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        paid_now = self.check_payments()  # a guest who says "I paid" gets their confirmation without waiting
        rows = []
        for b in self.repo.list_bookings(phone=ctx.phone, limit=3):
            prop = self.repo.get_property(b.property_id)
            row = {"id": b.id, "place": self._place_label(prop) if prop else "", "check_in": b.check_in.isoformat(),
                   "check_out": b.check_out.isoformat(), "rooms": b.rooms, "status": status_name(b.status, "en"),
                   "total": b.total, "paid": b.paid_amount, "balance_at_property": b.balance}
            if b.status == "hold" and b.payment_link_url:
                row["payment_link"] = b.payment_link_url
            rows.append(row)
        out: dict[str, Any] = {"bookings": rows} if rows else {"bookings": [], "note": "this guest has no booking yet"}
        if paid_now:
            out["note"] = "a payment was just found and the confirmation message was sent automatically; do not repeat its details"
        return out

    def _tool_resend(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        self._set_style(ctx.st, a)
        now = self.clock.now()
        live = [b for b in self.repo.list_bookings(phone=ctx.phone, status="hold", limit=5)
                if b.payment_link_url and b.hold_expires_at and b.hold_expires_at > now]
        if not live:
            return {"error": "no booking is waiting for payment (it may have expired or been paid); "
                             "use my_bookings to see its status, or start a new booking"}
        b = live[0]
        minutes = max(1, int((b.hold_expires_at - now).total_seconds() // 60))
        ctx.pay_message = t("pay", ctx.st.get("lang", "en"), id=b.id, minutes=minutes, advance=f"{b.advance:,}",
                            url=b.payment_link_url)
        return {"ok": True, "booking_id": b.id, "minutes_left": minutes,
                "note": "the link is sent automatically after your reply; do not write it"}
