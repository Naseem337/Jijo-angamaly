"""Guardrails on the AI's OWN text (docs/AI_AGENT_DESIGN.md section 6). Tool messages are never checked or changed.

    a) every ₹ amount, date and room count must appear in this turn's tool facts / tool messages, FACTS, the saved
       enquiry, or the guest's own messages
    b) "booked / confirmed / reserved" only when the enquiry (or a booking of this guest) is paid
    c) no promise words ("will contact you", "get back", "one moment")
       -> a), b), c): ONE retry with the exact problem told to the AI (turn.py); still bad -> handoff text + staff alert
    d) stripped quietly: the payment provider's name, booking ids, non-Latin script, links not from this turn, the
       guest's name
    e) a text identical to one already sent in this conversation is dropped

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
        piece = PROVIDER.sub("", BOOKING_ID.sub("", piece))
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
    facts = "\n".join([bot._facts(), enq.render(bot._place_label(prop) if prop else ""), days])
    tools = "\n".join([json.dumps(c.get("result"), ensure_ascii=False, default=str) for c in calls] + tool_texts)
    paid = enq.paid or any(b.status == "paid" for b in bot.repo.list_bookings(phone=phone, limit=20))
    return Sources(tools=tools, facts=facts, guest="\n".join([*said, guest_text]), paid=paid,
                   names=guest_names(bot, phone, enq))


def is_repeat(bot: Any, phone: str, text: str) -> bool:
    """e) This exact text already went to the guest in this conversation."""
    return bot._already_sent(phone, text)


