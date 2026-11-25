"""Guardrails on the AI's OWN text (docs/AI_AGENT_DESIGN.md section 6). Tool messages are never checked or changed.

    a) every ₹ amount, date and room count must appear in this turn's tool facts / tool messages, FACTS, the saved
       enquiry, or the guest's own messages
    b) "booked / confirmed / reserved" only when the enquiry (or a booking of this guest) is paid
    c) no promise words ("will contact you", "get back", "one moment")
       -> a), b), c): ONE retry with the exact problem told to the AI (turn.py); still bad -> handoff text + staff alert
    d) stripped quietly: the payment provider's name, booking ids, non-Latin script, links not from this turn, the
       guest's name
    e) a text identical to one already sent in this conversation is dropped
    f) a sentence where the AI asks for booking details (place, dates, persons, rooms, name, mobile) is dropped: the
       code's open step asks with the owner's words (turn.py)
    g) when a tool sent messages this turn, the AI's line is dropped if it has an amount, a link, a date, the words
       summary / payment / advance / link, or more than 25 words (it would repeat the tools' messages)
    fix round 2 (sentences dropped from the AI's own line): the hold or the link's expiry explained; "sent / shared /
       resent / passed your question / informed the team" when no sending tool ran; "How may I assist you" anywhere
       but the first greeting (nothing left -> "Hi sir."); "owner" is said as "team"
    fix round 3 (short, human replies): more than 2 sentences / 30 words, or a banned phrase ("anything else", "feel
       free", "system", "owner" ...) -> ONE retry (too long: "Too long. Say the same in under 20 words ..."); still bad ->
       the banned sentences are dropped and a reply still too long keeps its first sentence (never a handoff)

Regex here only reads the AI's own reply and code-made facts; nothing here decides what the GUEST meant (the guest's
messages are only searched for the same numbers and dates the AI wrote).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.resort.chat import english_letters_only
from app.resort.flow import BOOKING_ID, INDIC

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
        r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?")  # whole month words only ('Marari 5' is not a date)
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
DAY_MONTH = re.compile(rf"\b{_DAY}\s*(?:of\s+)?{_MON}", re.IGNORECASE)
MONTH_DAY = re.compile(rf"\b{_MON}\s*{_DAY}\b", re.IGNORECASE)
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b")  # dd/mm (India)
AMOUNT = re.compile(r"(?:₹|\brs\.?|\binr)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:/-|rupees\b|rs\b)", re.IGNORECASE)
ROOM_COUNT = re.compile(r"\b(\d{1,3})\s*(?:rooms?\b|room\(s\))", re.IGNORECASE)
ROOM_KEY = re.compile(r'"(?:rooms|count|rooms_needed|free)"\s*:\s*(\d{1,3})\b')  # a room count in a tool's JSON facts
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
BOOKED = re.compile(r"\b(?:booked|confirmed|reserved|booking is confirm\w*|confirm(?:ed)? your (?:booking|room)s?)\b",
                    re.IGNORECASE)
# a sentence that says it is NOT (yet) booked, or only after payment, is true and allowed
NOT_YET = re.compile(r"\b(?:not|n't|until|till|once|after|when|only|yet|before|illa|alla|nahi)\b", re.IGNORECASE)
PROMISE = re.compile(r"\b(?:will (?:contact|call|reach|get in touch with|message|inform|update|let) you|contact you"
                     r"|get back|call you back|one moment|a moment|just a (?:minute|second|sec)|give me a (?:minute|moment)"
                     r"|please wait|let me check)\b", re.IGNORECASE)
PROVIDER = re.compile(r"[ \t]*\b(?:(?:via|through|using|on|by)[ \t]+)?(?:razor[ \t]?pay|rzp)\b", re.IGNORECASE)
LINK = re.compile(r"https?://\S+")
# f) the AI asking for booking details itself (the code's open step asks with the owner's words): a sentence that asks
# (a question mark or a request word) AND names a booking detail. Only the AI's own reply is read here.
ASKING = re.compile(r"\?|\b(?:please|pls|plz|kindly|share|send|tell me|let me know|provide|confirm|could you|can you|"
                    r"may i (?:know|have)|need your|parayamo|parayu|tharamo|tharu|ayakkamo|ayakku|ariyikkamo|sollunga|"
                    r"batao|bataiye)\b", re.IGNORECASE)
DETAIL = re.compile(r"\b(?:(?:which|what|preferred)\s+(?:resort|place|location|property)|locations?|where\b.{0,30}\b(?:stay"
                    r"|looking)|evide|dates?|check[- ]?(?:in|out)|how many (?:nights|days|persons|people|adults|guests|pax"
                    r"|rooms)|(?:number|no\.?) of (?:nights|persons|people|adults|guests|rooms)|adults|persons|guests|pax|"
                    r"ethra (?:per|peru|aal|rooms?|divasam)|(?:your|the|full) name|name and|mobile|phone number|contact "
                    r"(?:number|details)|whatsapp number)\b", re.IGNORECASE)
TOOL_WORDS = re.compile(r"\b(?:summary|payment|advance|link)\b", re.IGNORECASE)
REPEAT_MAX_WORDS = 25
# fix round 2 (all read the AI's own reply only)
GREETING_ASK = re.compile(r"\bhow (?:may|can|could) i (?:assist|help)(?: you)?\b", re.IGNORECASE)
MID_CHAT_HI = "Hi sir."
SENT_CLAIM = re.compile(r"\b(?:sent|re-?sent|shared|forwarded|passed (?:on )?(?:it|your|the|this)\b|passed it on|"
                        r"informed the (?:owner|team|manager|staff))", re.IGNORECASE)
HOLD_TALK = re.compile(r"\b(?:expir\w*|valid (?:for|till|until)|time limit|hold (?:is|will|ends|runs)|"
                       r"held (?:for|until|till))", re.IGNORECASE)
MINUTES = re.compile(r"\b\d+\s*(?:more\s+)?min(?:ute)?s?\b", re.IGNORECASE)
LINK_WORDS = re.compile(r"\b(?:link|hold|held|open|pay\w*|block\w*|reserv\w*)\b", re.IGNORECASE)
OWNER = re.compile(r"\b(?:the\s+)?owner(?:'s|s)?\b", re.IGNORECASE)


@dataclass
class Sources:
    """Everything a figure in the AI's reply may come from."""
    tools: str = ""  # this turn's tool results (facts) + the tool messages queued for the guest
    facts: str = ""  # the FACTS block, the saved enquiry, today / tomorrow
    guest: str = ""  # the guest's messages in this conversation
    paid: bool = False  # the enquiry or a booking of this guest is paid
    names: list[str] = field(default_factory=list)  # the guest's name(s), never written to them

    @property
    def code(self) -> str:
        return self.tools + "\n" + self.facts

    @property
    def everything(self) -> str:
        return self.code + "\n" + self.guest


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def numbers_in(text: str) -> set[float]:
    return {n for m in NUMBER.findall(text) if (n := _num(m)) is not None}


def dates_in(text: str) -> set[tuple[int, int]]:
    """(month, day) of every date written in the text."""
    out: set[tuple[int, int]] = set()
    for d, m in DAY_MONTH.findall(text):
        out.add((MONTHS.index(m[:3].lower()) + 1, int(d)))
    for m, d in MONTH_DAY.findall(text):
        out.add((MONTHS.index(m[:3].lower()) + 1, int(d)))
    for _, m, d in ISO_DATE.findall(text):
        out.add((int(m), int(d)))
    for d, m in SLASH_DATE.findall(text):
        if 1 <= int(m) <= 12:
            out.add((int(m), int(d)))
    return {(m, d) for m, d in out if 1 <= m <= 12 and 1 <= d <= 31}


def problems(reply: str, src: Sources) -> list[str]:
    """What is wrong with the AI's reply (a, b, c), each said so the AI can fix it. [] = fine."""
    found: list[str] = []
    known = numbers_in(src.everything)
    for a, b in AMOUNT.findall(reply):
        raw = a or b
        if (value := _num(raw)) is not None and value not in known:
            found.append(f"Your reply says ₹{raw} but no tool result, FACTS or guest message gave that amount. "
                         "Amounts come only from tools (get_price); never calculate one.")
    known_dates = dates_in(src.everything)
    shown = []
    for m, d in sorted(dates_in(reply)):
        if (m, d) not in known_dates:
            shown.append(f"{d} {MONTHS[m - 1].title()}")
    if shown:
        found.append(f"Your reply says the date(s) {', '.join(shown)} but no tool result, FACTS or guest message gave "
                     "that date. Use only the dates of the enquiry (update_enquiry) or what the guest wrote.")
    known_rooms = {float(n) for n in ROOM_COUNT.findall(src.everything) + ROOM_KEY.findall(src.code)}
    for raw in ROOM_COUNT.findall(reply):
        if float(raw) not in known_rooms:
            found.append(f"Your reply says {raw} room(s) but no tool result gave that. Room counts come only from "
                         "update_enquiry.")
    if not src.paid:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply):
            if (hit := BOOKED.search(sentence)) and not NOT_YET.search(sentence):
                found.append(f'Your reply says "{hit.group(0)}" but nothing is paid yet. Never say booked, confirmed or '
                             "reserved before the system confirms the payment.")
                break
    if hit := PROMISE.search(reply):
        found.append(f'Your reply says "{hit.group(0)}": never promise a call, a wait or a later answer. Answer now, or '
                     "use talk_to_owner.")
    return found


def retry_message(found: list[str]) -> str:
    """The note fed back to the AI for its ONE retry."""
    return ("[CHECK] Your reply was NOT sent to the guest:\n- " + "\n- ".join(found)
            + "\nWrite the reply again without these problems (call a tool if you need the fact).")


def clean(reply: str, src: Sources) -> str:
    """d) Strip what the guest must never see from the AI's own text. '' when nothing is left."""
    if not any(line.strip() and not INDIC.search(line) for line in reply.split("\n")):
        return ""  # nothing in English letters at all (english_letters_only would put a 'type again' ask here)
    text = english_letters_only(reply)
    code = src.code
    pieces = []  # links are kept whole (only when this turn's tools gave them); the words around them are cleaned
    for i, piece in enumerate(re.split(r"(https?://\S+)", text)):
        if i % 2:
            pieces.append(piece if piece.rstrip(".,!?)") in code else "")
            continue
        piece = team_not_owner(PROVIDER.sub("", BOOKING_ID.sub("", piece)))
        for name in src.names:
            piece = re.sub(rf",?[ \t]*\b{re.escape(name)}\b", "", piece, flags=re.IGNORECASE)
        pieces.append(piece)
    text = "".join(pieces)
    text = re.sub(r"[ \t]+([.,!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text.strip() if re.search(r"[A-Za-z0-9]", text) else ""


# ------------------------------------------------------------------ building the sources


def guest_names(bot: Any, phone: str, enq: Any) -> list[str]:
    """The guest's name and first name (3+ letters), longest first."""
    names: set[str] = set()
    guest = bot.repo.get_guest(phone)
    for raw in (getattr(guest, "name", "") or "", (enq.contact or {}).get("name") or ""):
        raw = " ".join(str(raw).split())
        if sum(ch.isalpha() for ch in raw) >= 3:
            names.add(raw)
            first = raw.split()[0]
            if len(first) >= 3 and first.isalpha():
                names.add(first)
    return sorted(names, key=len, reverse=True)


def sources(bot: Any, phone: str, enq: Any, calls: list[dict[str, Any]], tool_texts: list[str],
            guest_text: str) -> Sources:
    rows = bot.repo.list_chat(phone, limit=200)
    said = [m.text for m in rows[bot._session_start(rows, by_time=True):] if m.direction == "in"]
    today = bot._today()
    days = "\n".join((today + timedelta(days=i)).isoformat() for i in range(2))
    prop = bot.repo.get_property(enq.place) if enq.place else None
    rooms = [f"{n} rooms" for n in {enq.rooms, (enq.checked or {}).get("rooms")} if n]  # the saved room counts
    facts = "\n".join([bot._facts(), enq.render(bot._place_label(prop) if prop else ""), days, *rooms])
    tools = "\n".join([json.dumps(c.get("result"), ensure_ascii=False, default=str) for c in calls] + tool_texts)
    paid = enq.paid or any(b.status == "paid" for b in bot.repo.list_bookings(phone=phone, limit=20))
    return Sources(tools=tools, facts=facts, guest="\n".join([*said, guest_text]), paid=paid,
                   names=guest_names(bot, phone, enq))


def asks_details(sentence: str) -> bool:
    """f) The AI's sentence asks the guest for a booking detail (place, dates, persons, rooms, name, mobile)."""
    return bool(ASKING.search(sentence) and DETAIL.search(sentence))


def drop_sentences(reply: str, bad: Any) -> str:
    """The AI's reply without its sentences for which bad(sentence) is true. '' when nothing is left."""
    lines = []
    for line in reply.split("\n"):
        kept = [s for s in re.split(r"(?<=[.!?])\s+", line) if s.strip() and not bad(s)]
        if kept:
            lines.append(" ".join(kept))
    text = "\n".join(lines).strip()
    return text if re.search(r"[A-Za-z0-9]", text) else ""


def drop_detail_asks(reply: str) -> str:
    """f) Remove the AI's own sentences that ask for booking details: the code's open step asks with the owner's words.
    '' when nothing is left."""
    return drop_sentences(reply, asks_details)


def drop_hold_talk(reply: str) -> str:
    """Never explain the hold or when the link expires ('within 15 minutes', 'open for 10 more minutes', 'expired')."""
    return drop_sentences(reply, lambda s: bool(HOLD_TALK.search(s) or (MINUTES.search(s) and LINK_WORDS.search(s))))


def drop_false_sent(reply: str) -> str:
    """No sending tool ran this turn: a sentence that says something was sent / shared / resent / passed on / the team
    informed is not true and is dropped."""
    return drop_sentences(reply, lambda s: bool(SENT_CLAIM.search(s)))


def mid_chat_greeting(reply: str) -> str:
    """'How may I assist you?' is only the first greeting of a conversation: anywhere else that sentence is dropped, and
    a reply left empty by it becomes a short 'Hi sir.' (the code's open step follows)."""
    if not GREETING_ASK.search(reply):
        return reply
    return drop_sentences(reply, lambda s: bool(GREETING_ASK.search(s))) or MID_CHAT_HI


def team_not_owner(reply: str) -> str:
    """With the guest it is always 'the team', never 'the owner'."""
    def team(m: re.Match[str]) -> str:
        said = m.group(0)
        word = ("the " if said.lower().startswith("the") else "") + "team" + ("'s" if said.lower().endswith("'s") else "")
        return word[:1].upper() + word[1:] if said[:1].isupper() else word

    return OWNER.sub(team, reply)


def repeats_tools(reply: str) -> bool:
    """g) Tools sent messages this turn and the AI's line looks like a copy of them: an amount, a link, a date, the words
    summary / payment / advance / link, or more than 25 words."""
    return bool(AMOUNT.search(reply) or LINK.search(reply) or dates_in(reply) or TOOL_WORDS.search(reply)
                or len(reply.split()) > REPEAT_MAX_WORDS)


# ------------------------------------------------------------------ fix round 3: short, human replies (the AI's line only)

MAX_SENTENCES, MAX_WORDS = 1, 20  # fix round 5 (was 2 / 30)
TOO_LONG = "Say it in one short sentence, under 15 words."
BANNED_PHRASES = ("anything else", "is there anything", "other listings", "other hotels", "other options", "feel free",
                  "don't hesitate", "I'm sorry, I only", "I don't have details", "as an AI", "please note that",
                  "I can help you with", "system", "automatically", "the tool", "owner")
BANNED = re.compile(r"\b(?:" + "|".join(re.escape(p).replace("'", "['’]") for p in BANNED_PHRASES) + r")\b", re.IGNORECASE)


def sentences(reply: str) -> list[str]:
    return [s for line in reply.split("\n") for s in re.split(r"(?<=[.!?])\s+", line) if re.search(r"[A-Za-z0-9]", s)]


def too_long(reply: str) -> bool:
    """More than 1 sentence or 20 words."""
    return len(sentences(reply)) > MAX_SENTENCES or len(reply.split()) > MAX_WORDS


def banned_in(reply: str) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in BANNED.finditer(reply)))


def style_problems(reply: str) -> list[str]:
    """Too long or a banned phrase: said so the AI can fix it in its ONE retry. [] = fine."""
    found = [TOO_LONG] if reply and too_long(reply) else []
    found += [f'Never write "{p}". Say it another way, or leave it out.' for p in banned_in(reply)]
    return found


def fix_style(reply: str) -> str:
    """After the retry: sentences with a banned phrase are dropped, then a reply still too long keeps its first
    sentence. '' when nothing is left."""
    reply = drop_sentences(reply, lambda s: bool(BANNED.search(s)))
    if reply and too_long(reply):
        reply = sentences(reply)[0].strip()
    return reply


# ------------------------------------------------------------------ fix round 5: booking talk goes to update_enquiry

BOOKING_RETRY = "This is booking information. Call update_enquiry instead of answering yourself."
BOOKING_WORDS = re.compile(r"\b(?:propert(?:y|ies)|resorts?|rooms?|available|availability|located|book\w*|stay)\b",
                           re.IGNORECASE)
WE_HAVE_PROPERTIES = re.compile(r"\bwe (?:only )?have (?:\w+ ){0,3}?(?:propert(?:y|ies)|resorts?)\b", re.IGNORECASE)
WHICH_PLACE = re.compile(r"\b(?:which|what|ethu|evide|enga)\b.{0,40}\b(?:place|resort|property|location|town)|"
                         r"\bwhere\b.{0,30}\b(?:stay|looking|go)\b", re.IGNORECASE)


def place_names(bot: Any) -> list[str]:
    """Every resort and town name of the dashboard (and their aliases), switched on or off."""
    names: set[str] = set()
    for p in bot.repo.list_properties(active_only=False):
        names |= {n for n in bot._place_words(p) if len(n) >= 4}
    return sorted(names, key=len, reverse=True)


def booking_talk(reply: str, names: list[str]) -> bool:
    """The AI's own line answers booking information itself: it says 'we have properties', asks which place / resort,
    or names a dashboard resort / town together with rooms, a property, availability or a question."""
    if WE_HAVE_PROPERTIES.search(reply) or WHICH_PLACE.search(reply):
        return True
    named = any(re.search(rf"\b{re.escape(n)}\b", reply, re.IGNORECASE) for n in names)
    return named and bool(BOOKING_WORDS.search(reply) or "?" in reply)


TOOL_REF = re.compile(r"\b(?:photos?|pics?|pictures?|images?|map|here (?:is|are)|below|above|form|details|summary|"
                      r"link|sent|shared)\b", re.IGNORECASE)


def _content(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", text.lower()))


def restates_tools(reply: str, tool_texts: list[str]) -> bool:
    """Tools sent messages this turn and the AI's line only talks about them: it points at what was sent (photos, the
    map, the form, the link, 'here are', 'below') or half of its words are in those messages."""
    if TOOL_REF.search(reply):
        return True
    mine = _content(reply)
    return bool(mine) and len(mine & _content(" ".join(tool_texts))) * 2 >= len(mine)


def is_repeat(bot: Any, phone: str, text: str) -> bool:
    """e) This exact text already went to the guest in this conversation."""
    return bot._already_sent(phone, text)


