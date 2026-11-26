"""Guards on the AI's reply() text (AI-first simplification, 2026-11-26). Tool messages are never checked or changed.

Only these checks:
    a) every ₹ amount must appear in a tool result, FACTS or the guest's messages anywhere in THIS CONVERSATION (an
       amount the guest typed, like 8000, may always be repeated)
    b) "booked / confirmed / reserved" only when a booking of this guest is paid; "a room is free / available"
       only when a tool returned a free room THIS turn
       -> a), b): ONE retry with the problem told to the AI (turn.py); still flagged -> only the offending sentence is
          removed and the rest goes. A non-empty answer is never replaced by a fallback.
    c) stripped quietly: the payment provider's name, booking ids, links no tool or FACTS gave

Regex here only reads the AI's own reply and code-made facts; nothing here decides what the GUEST meant (the guest's
messages are only searched for the same amounts the AI wrote).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.resort.agent import conversation
from app.resort.flow import BOOKING_ID

AMOUNT = re.compile(r"(?:₹|\brs\.?|\binr)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:/-|rupees\b|rs\b)", re.IGNORECASE)
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
BOOKED = re.compile(r"\b(?:booked|confirmed|reserved|booking is confirm\w*|confirm(?:ed)? your (?:booking|room)s?)\b",
                    re.IGNORECASE)
# a sentence that says it is NOT (yet) booked, or only after payment, is true and allowed
NOT_YET = re.compile(r"\b(?:not|n't|until|till|once|after|when|only|yet|before|illa|alla|nahi)\b", re.IGNORECASE)
# "pay ... to confirm your booking", "it will be confirmed": the future, true before payment (live replay 2026-11-26)
FUTURE = re.compile(r"\b(?:to|will(?: be)?|can be|would be)\s+$", re.IGNORECASE)
PROVIDER = re.compile(r"[ \t]*\b(?:(?:via|through|using|on|by)[ \t]+)?(?:razor[ \t]?pay|rzp)\b", re.IGNORECASE)
LINK = re.compile(r"https?://\S+")
SENTENCE = re.compile(r"(?<=[.!?])\s+")
# "X is (also) free / available": a room claim (free parking or a free cancellation is not one)
FREE_CLAIM = re.compile(r"\b(?:is|are)\s+(?:also\s+|still\s+)?(?:free|available)\b", re.IGNORECASE)
ROOM_WORDS = re.compile(r"\b(?:rooms?|cottages?|suites?|for your dates|on your dates)\b", re.IGNORECASE)
INDIC = re.compile(r"[ऀ-ॿ஀-௿ഀ-ൿ]")  # Hindi, Tamil, Malayalam script
AMOUNTS_KEY = "agent_amounts"  # guest state: the numbers tool results gave in this conversation


@dataclass
class Sources:
    """Everything an amount in the AI's reply may come from (this whole conversation)."""
    code: str = ""  # tool results of this conversation + our own messages + FACTS + the saved enquiry
    guest: str = ""  # the guest's messages in this conversation
    paid: bool = False  # a booking of this guest is paid
    free_now: bool = False  # a tool returned a free room this turn

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


# ------------------------------------------------------------------ a) b) per sentence


def sentence_problem(sentence: str, src: Sources, known: set[float] | None = None) -> str:
    """What is wrong with one sentence of the reply ('' = fine), said so the AI can fix it."""
    known = numbers_in(src.everything) if known is None else known
    if INDIC.search(sentence):  # the bot writes English letters only (the guest may write any script)
        return ("Write in English letters only: Malayalam script → Manglish, Tamil → Tanglish, Hindi → Hinglish "
                "(the same words in English letters).")
    for a, b in AMOUNT.findall(sentence):
        raw = a or b
        if (value := _num(raw)) is not None and value not in known:
            return (f"₹{raw} is not in any tool result, FACTS or guest message of this conversation. Amounts come only "
                    "from tools (get_price); never calculate one.")
    if not src.paid and (hit := BOOKED.search(sentence)) and not NOT_YET.search(sentence) \
            and not FUTURE.search(sentence[:hit.start()]):
        return (f'"{hit.group(0)}": nothing is paid yet. Never say booked, confirmed or reserved before the payment is '
                "received.")
    if not src.free_now and FREE_CLAIM.search(sentence) and ROOM_WORDS.search(sentence) \
            and not NOT_YET.search(sentence):
        return ("No tool returned a free room this turn: never say a room is free or available without one "
                "(update_enquiry or send_photos purpose consider).")
    return ""


def _sentences(reply: str) -> list[str]:
    return [s for line in reply.split("\n") for s in SENTENCE.split(line) if s.strip()]


def problems(reply: str, src: Sources) -> list[str]:
    """Every problem of the reply (a, b), one per bad sentence. [] = fine."""
    known = numbers_in(src.everything)
    return [p for s in _sentences(reply) if (p := sentence_problem(s, src, known))]


def drop_bad(reply: str, src: Sources) -> str:
    """The reply without its bad sentences (a, b): the rest goes. '' only when every sentence was bad."""
    known = numbers_in(src.everything)
    lines = []
    for line in reply.split("\n"):
        if not line.strip():
            lines.append("")
            continue
        kept = [s for s in SENTENCE.split(line) if s.strip() and not sentence_problem(s, src, known)]
        if kept:
            lines.append(" ".join(kept))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text if re.search(r"[A-Za-z0-9]", text) else ""


def retry_message(found: list[str]) -> str:
    """The tool result the AI gets for its ONE retry."""
    return "NOT sent to the guest:\n- " + "\n- ".join(found) + "\nCall reply() again without these problems."


# ------------------------------------------------------------------ c) stripped quietly


def clean(reply: str, src: Sources) -> str:
    """c) The payment provider's name, booking ids and links no tool or FACTS gave are taken out."""
    pieces = []
    for i, piece in enumerate(re.split(r"(https?://\S+)", reply)):
        if i % 2:
            pieces.append(piece if piece.rstrip(".,!?)") in src.code else "")
        else:
            pieces.append(PROVIDER.sub("", BOOKING_ID.sub("", piece)))
    text = "".join(pieces)
    text = re.sub(r"[ \t]+([.,!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text.strip() if re.search(r"[A-Za-z0-9]", text) else ""


# ------------------------------------------------------------------ building the sources


def remember_amounts(bot: Any, phone: str, conv: str, result: Any) -> None:
    """The numbers in a tool result are kept for the rest of this conversation (a price from an earlier turn may be
    repeated later)."""
    found = numbers_in(json.dumps(result, ensure_ascii=False, default=str))
    if not found:
        return
    st = bot.repo.get_state(phone)
    saved = st.get(AMOUNTS_KEY) or {}
    nums = set(saved.get("nums") or []) if saved.get("conv") == conv else set()
    st[AMOUNTS_KEY] = {"conv": conv, "nums": sorted(nums | found)[-400:]}
    bot.repo.set_state(phone, st)


def free_in(calls: list[dict[str, Any]]) -> bool:
    """A tool of this turn returned a free room."""
    for c in calls:
        facts = (c.get("result") or {}).get("facts") or {}
        if facts.get("available") is True or facts.get("also_free_for_your_dates"):
            return True
    return False


def sources(bot: Any, phone: str, enq: Any, calls: list[dict[str, Any]], tool_texts: list[str], guest_text: str,
            conv: str = "") -> Sources:
    rows = bot.repo.list_chat(phone, limit=200)
    current = rows[conversation.start_index(rows):]
    said = [m.text for m in current if m.direction == "in"]
    ours = [m.text for m in current if m.direction == "out"]
    saved = bot.repo.get_state(phone).get(AMOUNTS_KEY) or {}
    earlier = " ".join(str(n) for n in saved.get("nums") or []) if saved.get("conv") == conv else ""
    prop = bot.repo.get_property(enq.place) if enq.place else None
    facts = "\n".join([bot._facts(), enq.render(bot._place_label(prop) if prop else ""), earlier])
    tools = "\n".join([json.dumps(c.get("result"), ensure_ascii=False, default=str) for c in calls] + tool_texts + ours)
    paid = enq.paid or any(b.status == "paid" for b in bot.repo.list_bookings(phone=phone, limit=20))
    return Sources(code=tools + "\n" + facts, guest="\n".join([*said, guest_text]), paid=paid,
                   free_now=free_in(calls))
