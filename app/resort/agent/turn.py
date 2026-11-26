"""One guest turn (docs/AI_AGENT_DESIGN.md sections 1 and 4; AI-first simplification 2026-11-26).

    system prompt (cached static part + per-turn part) + the last 12 real messages + the new guest message (with a
    swipe-reply line before it)
      -> LLM <-> tools, at most 4 rounds of tool calls; tool results go back as compact JSON, and an earlier round's
         results shrink to one line each when a new round starts
      -> the guest sees ONLY what the AI passes to reply() plus what the sending tools queue (summary, payment link,
         paid confirmation, photos, room labels, glimpse, map), in the order they were called. Any other text the AI
         writes is a private note: logged, never sent.
      -> reply() text goes through guard.py: a wrong amount or 'booked' before payment -> ONE retry with the reason;
         still flagged -> only that sentence is removed. A non-empty answer is never replaced by a fallback.
      -> nothing for the guest at all -> the AI is asked once more ("Call reply() ..."); only when that is empty too:
         "Sorry, could you say that again?" (logged with the reason).

The model comes from the LLM chain (LLM_CHAIN) only: nothing here names one. Temperature 0.2, at most 300 output tokens
per call, no extended thinking. Token use per turn goes to usage.py. A failing LLM gets the ai_down text and one staff
alert per 30 minutes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.providers.llm import ChatMessage
from app.resort.agent import conversation, guard
from app.resort.agent import language as turn_language
from app.resort.agent import memory
from app.resort.agent import photo_map
from app.resort.agent import photos_plan as pp
from app.resort.agent import usage
from app.resort.agent.outbox import Outbox, near_same
from app.resort.agent.prompt import build_system_prompt
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import I18N_LANG, TOOL_SCHEMAS, AgentTools, towns_or
from app.resort.chat import RESTART_MARK
from app.resort.i18n import t

log = logging.getLogger(__name__)

MAX_ROUNDS = 4  # rounds of tool calls in one turn
MAX_REPLY_TOKENS = 300  # the reply is short; tool calls are small
HISTORY = 12  # real messages of the current conversation the AI sees (CURRENT ENQUIRY already holds the stay)
TEMPERATURE = 0.2
AI_DOWN_ALERT_GAP = timedelta(minutes=30)  # one staff alert per outage, not per message
SKIPPED_ROWS = ("[invoice]", "[template", "[image]", "[voice note: not understood")  # log lines, never shown to the AI
REPLY_AGAIN = "[SYSTEM] Nothing was sent to the guest. Call reply() with your answer to the guest."
SWIPE_MARK = "[Guest swiped-replied to"  # every swipe-reply note starts with it
WHICH_ROOM = "Which room do you mean?"  # a swipe-reply's last fallback: never 'say that again' (owner, 2026-11-26)


@dataclass
class TurnResult:
    outcome: str  # answered | asked_again | say_again | ai_down
    reply: str = ""  # what the AI sent with reply()
    rounds: int = 0  # rounds of tool calls that ran
    hit_limit: bool = False  # the AI still wanted tools after MAX_ROUNDS
    calls: list[dict[str, Any]] = field(default_factory=list)  # [{"name", "args", "result"}] in order
    sent: list[str] = field(default_factory=list)  # the outbox keys, in the order they went out
    error: str = ""
    guard: list[str] = field(default_factory=list)  # what the guard found wrong with the AI's reply() text
    retried: bool = False  # the AI got its one retry
    notes: list[str] = field(default_factory=list)  # text the AI wrote outside reply(): private, never sent
    # token use of the whole turn (usage.py logs it; never any guest text)
    model: str = ""
    llm_calls: int = 0
    input_tokens: int = 0  # cached ones included
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    def add_usage(self, response: Any) -> None:
        u = getattr(response, "usage", None)
        self.llm_calls += 1
        self.model = getattr(response, "model", "") or self.model
        if u is not None:
            self.input_tokens += u.input_tokens
            self.cache_read_tokens += u.cached_input_tokens
            self.cache_write_tokens += getattr(u, "cache_write_tokens", 0)
            self.output_tokens += u.output_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ------------------------------------------------------------------ what the AI sees


def _mask_links(text: str) -> str:
    """An old payment link must never be copied into a new answer."""
    return "\n".join(" ".join("[link]" if w.startswith("http") else w for w in line.split(" ")) for line in text.split("\n"))


def history(bot: Any, phone: str) -> list[ChatMessage]:
    """The last HISTORY real messages of the current conversation (a restart or a 24 h gap starts a new one). A run of
    photos is one short note; log lines, templates and old links never reach the AI."""
    rows = bot.repo.list_chat(phone, limit=200)
    items: list[tuple[str, str]] = []
    for m in rows[conversation.start_index(rows):]:
        text = m.text
        if text == RESTART_MARK or text.startswith(SKIPPED_ROWS):
            continue
        if m.direction == "out" and text.startswith("[photo] "):
            if items and items[-1][1].startswith("[photos sent:"):
                items[-1] = ("assistant", f"[photos sent: {int(items[-1][1].split(':')[1].strip(' ]')) + 1}]")
            else:
                items.append(("assistant", "[photos sent: 1]"))
            continue
        items.append(("user" if m.direction == "in" else "assistant", _mask_links(text[:700])))
    messages: list[ChatMessage] = []
    for role, text in items[-HISTORY:]:
        if messages and messages[-1].role == role:
            messages[-1].content += "\n" + text
        else:
            messages.append(ChatMessage(role, text))
    opening = []  # what we said before the guest's first message (the greeting after a restart): kept as a note
    while messages and messages[0].role != "user":
        opening.append(messages.pop(0).content)
    if opening and messages:
        said = " ".join(" ".join(opening).split())[:300]
        messages[0].content = f'[You already said: "{said}"]\n' + messages[0].content
    return messages


def greeted(bot: Any, phone: str) -> bool:
    """We already wrote to the guest in this conversation (a restart's greeting counts): it is not a fresh start."""
    rows = bot.repo.list_chat(phone, limit=200)
    return any(m.direction == "out" and m.text != RESTART_MARK for m in rows[conversation.start_index(rows):])


def swipe_note(bot: Any, phone: str, context_id: str | None) -> str:
    """A line for a swipe-reply: which earlier message the guest answered ('' when it is not a swipe-reply). Owner,
    2026-11-26: WhatsApp id -> photo id -> room from the photo map saved at sending (photo_map); photos sent before the
    map existed are looked up from the chat log."""
    if not context_id:
        return ""
    sent = bot.repo.photo_send(phone, context_id)
    quoted = bot.repo.chat_by_wamid(phone, context_id)
    what = "the photo" if (sent and sent["kind"] == "photo") or (quoted and quoted.text.startswith("[photo]"))         else "the label"
    if sent is not None:
        if sent["room_type_id"] == photo_map.RESORT:
            prop = bot.repo.get_property(sent["property_id"])
            return (f"{SWIPE_MARK} a general photo of the resort{f' ({prop.name})' if prop else ''}, not of a room. "
                    "Treat the message as a question about the resort, never as a room choice.]")
        rt = bot.repo.get_room_type(sent["room_type_id"])
        if rt is not None and rt.active:
            return _room_note(what, rt, sent["room_number"])
        if rt is not None:  # switched off in the dashboard: invisible to guests, never named
            return f"{SWIPE_MARK} an earlier message. If they mean a room, ask: \"{WHICH_ROOM}\"]"
    if quoted is None:  # an old message or one we never logged: the room is not known
        return f"{SWIPE_MARK} an earlier message that cannot be found. If they mean a room, ask: \"{WHICH_ROOM}\"]"
    if quoted.direction == "out":
        if room := bot._room_of_message(quoted.text):
            return _room_note(what, *room)
        if quoted.text.startswith("[photo]"):
            ref = quoted.text[len("[photo] "):].strip()
            if (found := photo_map.photo_room(bot.repo, ref)) and found[1] == photo_map.RESORT:
                return (f"{SWIPE_MARK} a general photo of the resort, not of a room. Treat the message as a question "
                        "about the resort, never as a room choice.]")
            return f"{SWIPE_MARK} a photo whose room is not known. If they mean a room, ask: \"{WHICH_ROOM}\"]"
        return f'{SWIPE_MARK} your message: "{quoted.text[:300]}"]'
    return f'[Guest swiped-replied to their own earlier message: "{quoted.text[:300]}"]'


def _room_note(what: str, rt: Any, number: str) -> str:
    label = f"Room {number} — {rt.name}" if number else rt.name
    pick = f'{{"room_type": "{rt.name}"' + (f', "room_number": "{number}"' if number else "") + "}"
    return (f"{SWIPE_MARK} {what} of {label}. Their message is about THIS room: choosing it in any words or language "
            f"('this one', 'that room', 'I need this', 'book this', 'ithu venam') → update_enquiry(room_picks=[{pick}]); "
            f"price → get_price(room_type=\"{rt.name}\"); a question → answer about this room.]")


def _with_guest_message(messages: list[ChatMessage], text: str, swipe: str) -> list[ChatMessage]:
    """The new guest message is the last one (it is usually in the chat log already), with the swipe line just before it."""
    text = text.strip()
    if not (messages and messages[-1].role == "user" and messages[-1].content.rstrip().endswith(text)):
        messages.append(ChatMessage("user", text))
    if swipe:
        last = messages[-1]
        cut = len(last.content.rstrip()) - len(text)
        last.content = last.content[:cut] + swipe + "\n" + text
    return messages


# ------------------------------------------------------------------ tool results as the AI reads them


def _filled(value: Any) -> bool:
    return value not in (None, "", [], {})


def compact_result(out: dict[str, Any]) -> str:
    """A tool result as the AI reads it this turn: compact JSON with only the fields that say something (empty facts,
    an empty `sent`, a missing next step or note are left out). `ok` always stays."""
    lean: dict[str, Any] = {"ok": out.get("ok")}
    for key, value in out.items():
        if key == "ok":
            continue
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if _filled(v)}
        if _filled(value):
            lean[key] = value
    return json.dumps(lean, ensure_ascii=False, separators=(",", ":"), default=str)


def summary_line(name: str, out: dict[str, Any]) -> str:
    """An older tool result (an earlier round of this turn): one line, e.g. 'update_enquiry: ok, rooms=2, sent
    photos:3, next ask_contact'. The latest round's results stay in full."""
    parts = [f"{name}: {'ok' if out.get('ok') else 'not ok'}"]
    facts = out.get("facts") or {}
    short = [(k, v) for k, v in facts.items() if isinstance(v, (str, int, float, bool)) and _filled(v)
             and len(str(v)) <= 24]
    parts += [f"{k}={v}" for k, v in short[:4]]
    if sent := [k for k in out.get("sent") or [] if not str(k).startswith("staff:")]:
        parts.append("sent " + ", ".join(map(str, sent)))
    if out.get("next_step"):
        parts.append(f"next {out['next_step']}")
    return "[earlier result] " + ", ".join(parts)


def _raw(response: Any) -> str:
    return (response.text or "").strip() if response is not None else ""


# ------------------------------------------------------------------ the turn


def run_turn(bot: Any, llm: Any, phone: str, text: str, outbox: Outbox, *, swipe: str = "") -> TurnResult:
    """Answer one guest message. The outbox is flushed at the end (a dry-run outbox only records)."""
    conv = conversation.remember(bot, phone)  # where this conversation began (restart / confirmed booking / 12 h gap)
    words = turn_language.load(bot, phone, conv["started_at"])  # never a language from another conversation
    enq = memory.refresh(bot, phone, conv)  # the saved enquiry: 7 days, old check dropped in a new conversation
    messages = _with_guest_message(history(bot, phone), text, swipe)
    fresh = len(messages) <= 1 and not greeted(bot, phone)
    system = build_system_prompt(bot, phone, enq, fresh=fresh, lock=words.lock)
    tools = AgentTools(bot, phone, outbox, guest_text=text, lang=words)
    result = TurnResult("answered")
    flagged: list[str] = []  # a reply() the guard sent back for its one retry (sent trimmed if no retry comes)
    repeats: list[str] = []  # replies refused because they repeat one of the bot's last 3 messages
    latest: list[tuple[ChatMessage, str]] = []  # the last round's tool results (full) and their one-line summaries

    def complete() -> Any:
        # while nothing has reached the guest this turn the AI MUST call a tool (live replay 2026-11-26: Haiku answered
        # in plain text, a private note, and the guest got 'say that again'); afterwards it may end with no call
        must = {} if outbox.spoke else {"tool_choice": "required"}
        response = llm.complete(system, messages, TOOL_SCHEMAS, MAX_REPLY_TOKENS, temperature=TEMPERATURE, **must)
        result.add_usage(response)
        return response

    def note(response: Any) -> None:
        if raw := _raw(response):  # the AI's own text outside reply(): a private note, never sent
            result.notes.append(raw)
            log.info("Agent note for %s (not sent): %s", phone[-4:], raw[:300])

    def sources() -> guard.Sources:
        facts_calls = [c for c in result.calls if c["name"] != "reply"]  # the guard's own retry note is no source
        return guard.sources(bot, phone, Enquiry.load(bot.repo, phone), facts_calls, outbox.texts, text,
                             conv["started_at"])

    def send(reply: str) -> None:
        # owner, 2026-11-26: the AI's own answer, the owner's form and the name / mobile ask are separate messages, in
        # that order; a form the bot sent among its last 3 messages is not sent again
        code_spoke = any(i.kind in ("text", "photos") and i.key != "ai" for i in outbox.queue + outbox.sent)
        for part in split_reply(reply):
            if CONTACT_ASK.fullmatch(part.strip()) and (ask_recent(bot, phone) or any(
                    i.key == "ask_contact" for i in outbox.queue + outbox.sent)):
                log.info("Agent: the name / mobile ask is among the last 3 messages for %s: not sent again", phone[-4:])
                continue
            if code_spoke and LONE_ACK.fullmatch(part.strip()):
                continue  # code already answered this turn: a bare 'Ok.' would be a message of its own
            if is_form(part) and form_recent(bot, phone, part):
                log.info("Agent: the same form is among the last 3 messages for %s: not sent again", phone[-4:])
                continue
            outbox.text("ai", part)
        result.reply = (result.reply + "\n" + reply).strip()

    def refused(reasons: list[str]) -> dict[str, Any]:
        """Owner, 2026-11-27 (safety): the guards left nothing worth sending. The AI gets ONE more try with the
        reasons; after that the open step (or the big-group message) goes, never 'say that again'."""
        if not repeats:
            repeats.append("; ".join(reasons))
            log.info("Agent: reply for %s removed (%s); the AI answers again", phone[-4:], "; ".join(reasons))
            return {"ok": False, "facts": {"problem": reasons},
                    "note": f"Your reply was removed because: {'; '.join(reasons)}. Answer the guest's last message "
                            "again without that."}
        line = fallback_line(bot, phone, tools, I18N_LANG[words.now])
        log.warning("Agent fallback for %s: '%s' (removed twice: %s | %s)", phone[-4:], line[:80], repeats[0],
                    "; ".join(reasons))
        result.outcome = "fallback"
        outbox.text("fallback", line)
        return {"ok": True, "facts": {"sent_to_guest": True}, "note": "A short answer went. Do not call reply() again."}

    def do_reply(args: dict[str, Any]) -> dict[str, Any]:
        """reply(text): the guest's message, through the guards (a, b: one retry; c: stripped)."""
        if tools.code_final:  # code already sent the owner's complete answer (big group): the AI adds nothing
            return {"ok": True, "facts": {"sent_to_guest": True},
                    "note": "The owner's message already went. Nothing more is sent this turn."}
        if tools.lang is not None:
            tools.lang.take(args.get("language"))  # the reply's language is this turn's language
        src = sources()
        raw = str(args.get("text") or "").strip()
        reply = guard.clean(raw, src)
        removed: list[str] = []  # owner, 2026-11-27: every removal is logged with its reason
        if reply != raw:
            removed.append("internal words, a link, a booking id or a pay ask that does not fit were taken out")
        # a line code already sent this turn (the availability line, the rooms sentence ...) is never written again
        code_lines = [ln for i in outbox.queue + outbox.sent if i.kind == "text" and i.key != "ai"
                      for ln in i.text.split("\n") if ln.strip()]
        if code_lines and reply:
            kept = "\n".join(ln for ln in reply.split("\n")
                             if not (ln.strip() and any(near_same(ln, c) for c in code_lines))).strip()
            if kept != reply:
                removed.append("lines repeated what code already sent this turn")
            if not kept and outbox.spoke:  # code already answered this turn: nothing more is needed
                log.info("Agent: reply for %s only repeated code's messages; nothing more sent", phone[-4:])
                return {"ok": True, "facts": {"sent_to_guest": True}, "note": "Code's messages already answer it."}
            reply = kept
        # owner, 2026-11-27: the team's numbers may be in 2 of the bot's last 5 messages; a 3rd time only the NUMBERS
        # are replaced (never the sentence); the guest asking for them (guest_asked_contact) always gets them
        tails = guard.team_tails(bot)
        if reply and not args.get("guest_asked_contact") \
                and guard.numbers_count_recent(bot, phone, tails) >= guard.NUMBERS_ALLOWED:
            swapped = guard.replace_team_numbers(reply, tails)
            if swapped != reply:
                removed.append("the team's numbers were already given twice")
                reply = swapped
        # never a text identical to one of the bot's last 3 messages
        if reply and any(near_same(reply, old) for old in pp.recent_texts(bot, phone, RECENT)):
            removed.append("it repeats one of your last 3 messages word for word")
            reply = ""
        if removed:
            log.info("Guard for %s: %s", phone[-4:], "; ".join(removed))
        if not reply or (removed and LONE_ACK.fullmatch(reply.strip())):
            return refused(removed or ["the reply was empty"])
        found = guard.problems(reply, src)
        if found and not result.retried:  # ONE retry with the reason
            result.retried, result.guard = True, result.guard + found
            flagged[:] = [reply]
            log.info("Guard: retry for %s: %s", phone[-4:], found)
            return {"ok": False, "facts": {"problem": found}, "note": guard.retry_message(found)}
        flagged.clear()
        if found:  # still flagged after the retry: only the offending sentences go
            result.guard = result.guard + found
            reply = guard.drop_bad(reply, src)
            log.info("Guard: removed flagged sentence(s) for %s: %s", phone[-4:], found)
            if not reply or LONE_ACK.fullmatch(reply.strip()):
                return refused(found)
        send(reply)
        return {"ok": True, "facts": {"sent_to_guest": True}, "note": "Sent. Do not call reply() again this turn."}

    def ask() -> None:
        """One LLM reply, with every tool call it makes run and fed back (MAX_ROUNDS for the whole turn). A round in
        which reply() went out ends the turn: no further LLM call is needed."""
        response = complete()
        while response.tool_calls:
            if result.rounds >= MAX_ROUNDS:
                result.hit_limit = True
                log.warning("Agent turn for %s still wanted tools after %d rounds", phone[-4:], MAX_ROUNDS)
                break
            note(response)
            for msg, short in latest:  # an older round's results: one line each (the newest round stays in full)
                msg.content = short
            latest.clear()
            messages.append(response.to_message())
            replied = False
            for call in response.tool_calls:
                if call.arguments_error:  # the model did not send a JSON object: it is told, the tool does not run
                    out = tools._result(False, {"problem": call.arguments_error}, len(outbox.keys),
                                        Enquiry.load(bot.repo, phone), "Call the tool again with a JSON object.")
                elif call.name == "reply":
                    out = do_reply(call.arguments or {})
                    replied = replied or bool(out["ok"])
                else:
                    out = tools.call(call.name, call.arguments)
                    guard.remember_amounts(bot, phone, conv["started_at"], out)
                result.calls.append({"name": call.name, "args": call.arguments, "result": out})
                msg = ChatMessage.tool_result(call.id, compact_result(out), is_error=not out["ok"])
                messages.append(msg)
                latest.append((msg, summary_line(call.name, out)))
            result.rounds += 1
            if replied:
                return
            response = complete()
        note(response)

    failure: Exception | None = None
    try:
        if llm is None:
            raise RuntimeError("no LLM configured")
        ask()
        if flagged:  # the AI never retried the flagged reply: it goes without the offending sentences
            if trimmed := guard.drop_bad(flagged[0], sources()):
                send(trimmed)
            flagged.clear()
        if not outbox.spoke:  # nothing for the guest: the AI is asked once more
            result.outcome = "asked_again"
            log.info("Agent: nothing sent for %s; asking the AI once more", phone[-4:])
            messages.append(ChatMessage("user", REPLY_AGAIN))
            ask()
    except Exception as exc:  # an LLM outage or a crash must never leave the guest unanswered
        failure = exc
        log.warning("AI turn failed: %s", exc)

    enq = Enquiry.load(bot.repo, phone)  # the tools may have changed it
    enq.language = words.now  # this turn's language (the AI's, else this conversation's last, else en)
    lang = I18N_LANG[enq.language]
    turn_language.save(bot, phone, words)
    if failure is not None:
        result.outcome, result.error = "ai_down", f"{type(failure).__name__}: {failure}"
        if not outbox.spoke:
            outbox.text("ai_down", bot._owner_msg("ai_down", {"lang": lang, "form_place": enq.place}))
        _alert_ai_down(bot, outbox, phone, text, enq)
    outbox.dedupe()  # never the same (or nearly the same) text twice in one turn
    drop_recent(bot, phone, outbox, link_asked="send_payment_link" in tools.ran)
    if failure is None and not outbox.spoke:  # truly empty twice: a fallback, logged with the reason
        if SWIPE_MARK in swipe:  # a swipe-reply never gets 'say that again' (owner, 2026-11-26)
            result.outcome = "say_again"
            outbox.text("say_again_short", WHICH_ROOM)
        elif unreadable(text):  # owner, 2026-11-27: 'say that again' ONLY for a message with nothing to read
            result.outcome = "say_again"
            outbox.text("say_again_short", t("say_again_short", lang))
        else:
            result.outcome = "fallback"
            outbox.text("fallback", fallback_line(bot, phone, tools, lang))
        log.warning("Agent: %s for %s: the AI sent nothing twice (notes: %s)", result.outcome, phone[-4:],
                    " | ".join(result.notes)[:300] or "none")
    outbox.flush()
    result.sent = outbox.keys
    usage.record(bot, phone, result, dry_run=outbox.dry_run)
    return result


FORM_HEAD = re.compile(r"(?:to check (?:the price|availability),\s*)?please share these details\s*:?", re.IGNORECASE)
CONTACT_ASK = re.compile(r"please share your name and mobile number to proceed\.?", re.IGNORECASE)
LOCATION_HEAD = re.compile(r"sure,?\s*kindly confirm the location\s*📍?", re.IGNORECASE)
OWNER_LINE = re.compile(r"(?:please contact the team directly\s*:|for more detailed discussion,?\s*kindly call)",
                        re.IGNORECASE)
FORM_LINE = re.compile(r"^\s*(?:check[ -]?in|check[ -]?out|number of adults|number of rooms)\b.*:\s*$", re.IGNORECASE)
# a sentence of the AI's own line that asks again for what the form / ask / location question asks
REASK = re.compile(r"\b(?:share|provide|send|tell|give|let (?:me|us) know|confirm|type)\b[^.?!]*\b(?:dates?|check[- ]?in|"
                   r"check[- ]?out|details|adults|persons|people|guests|rooms?|name|mobile|number|location|place)\b",
                   re.IGNORECASE)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def is_form(text: str) -> bool:
    return bool(FORM_HEAD.match(text.split("\n", 1)[0].strip()))


def _cut_lines(reply: str) -> list[str]:
    """Each line, cut where an owner block starts inside it ('Sorry, 12 Nov has passed. Please share these details:'
    -> two lines), and after the name / mobile ask."""
    out: list[str] = []
    for line in reply.split("\n"):
        while True:
            hits = [m for rx in (FORM_HEAD, CONTACT_ASK, LOCATION_HEAD, OWNER_LINE) if (m := rx.search(line))]
            first = min(hits, key=lambda m: m.start()) if hits else None
            if first is None:
                out.append(line)
                break
            if first.start() > 0 and line[:first.start()].strip():
                out.append(line[:first.start()].rstrip())
                line = line[first.start():]
                continue
            if first.re is CONTACT_ASK and line[first.end():].strip():
                out.append(line[:first.end()])
                line = line[first.end():].lstrip()
                continue
            out.append(line)
            break
    return out


def split_reply(reply: str) -> list[str]:
    """Owner, 2026-11-26 (every reply): the AI's own line first, then each owner block as its OWN WhatsApp message: the
    location question, the team's numbers line, the form (head + lines), the name / mobile ask. When a block follows,
    the AI's line never asks for the same thing again (those sentences are dropped). Only the AI's own reply is read
    here, never the guest's text."""
    answer: list[str] = []
    location: list[str] = []
    owner: list[str] = []
    form: list[str] = []
    ask: list[str] = []
    mode = ""
    for line in _cut_lines(reply):
        text = line.strip()
        if FORM_HEAD.match(text):
            mode = "form"
            form.append(text)
        elif CONTACT_ASK.match(text):
            mode = ""
            ask.append(text)
        elif LOCATION_HEAD.match(text):
            mode = "location"
            location.append(text)
        elif OWNER_LINE.match(text):
            mode = ""
            owner.append(text)
        elif mode == "form" and (not text or FORM_LINE.match(line)):
            form.append(line.rstrip())
        elif mode == "location" and text and len(location) == 1:
            location.append(text)  # the towns line ('Alleppey or Kanthalloor ?')
            mode = ""
        else:
            mode = ""
            answer.append(line)
    own = "\n".join(answer).strip()
    if own and (form or ask or location):
        kept = []
        for line in own.split("\n"):
            sentences = [x for x in SENTENCE_END.split(line) if not (x.strip() and REASK.search(x))]
            if " ".join(sentences).strip() or not line.strip():
                kept.append(" ".join(sentences).strip())
        own = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
        if own and not re.search(r"[A-Za-z0-9]", own):
            own = ""
    if owner and any(OWNER_LINE.match(o) for o in owner):
        return ["\n".join(owner).strip()]  # owner, 2026-11-26: a handoff is ONLY the team's numbers, nothing else
    parts = [own, "\n".join(location).strip(), "\n".join(owner).strip(), "\n".join(form).strip(),
             " ".join(ask).strip()]
    return [x for x in parts if x]


LONE_ACK = re.compile(r"(?:ok(?:ay)?|got it|sure|noted|fine|alright|sheri|seri|theek hai)[.!]?\s*(?:👍)?",
                      re.IGNORECASE)


def ask_recent(bot: Any, phone: str) -> bool:
    """The name / mobile ask is among the bot's last 3 messages."""
    outs = [m.text for m in bot.repo.list_chat(phone, limit=20) if m.direction == "out"][-3:]
    return any(CONTACT_ASK.search(o) for o in outs)


def form_recent(bot: Any, phone: str, form: str) -> bool:
    """The same form (same missing lines) is among the bot's last 3 messages."""
    want = {ln.strip().lower() for ln in form.split("\n")[1:] if ln.strip()}
    outs = [m.text for m in bot.repo.list_chat(phone, limit=20) if m.direction == "out"][-3:]
    for text in outs:
        if is_form(text) or "please share these details" in text.lower():
            got = {ln.strip().lower() for ln in text.split("\n") if FORM_LINE.match(ln)}
            if got == want:
                return True
    return False


def unreadable(text: str) -> bool:
    """Nothing to read: empty, or no letter or digit in any script (random symbols)."""
    return not any(ch.isalnum() for ch in (text or ""))


def fallback_line(bot: Any, phone: str, tools: Any, lang: str) -> str:
    """What goes when the AI's answer could not be used twice: the big-group message again (this enquiry), else the
    open step of the enquiry, else the greeting's question. Never 'say that again'."""
    enq = Enquiry.load(bot.repo, phone)
    prop = bot.repo.get_property(enq.place) if enq.place else None
    rec = tools._big_group()
    if prop is not None and rec and enq.check_in and (rec.get("prop"), rec.get("ci"), rec.get("co")) == (
            prop.id, enq.check_in.isoformat(), enq.check_out.isoformat() if enq.check_out else None):
        return t("group_too_big", lang, free_rooms=rec["free"], max_people=rec["most"], numbers=tools._numbers(prop))
    return step_line(bot, phone, lang) or "How may I assist you?"


def step_line(bot: Any, phone: str, lang: str) -> str:
    """The open step's owner text ('' when no step is open)."""
    step = Enquiry.load(bot.repo, phone).next_step
    if step == "ask_location":
        return t("ask_location", lang, towns=towns_or(bot))
    if step == "ask_details":
        return t("form", lang)
    if step == "ask_extra_bed":
        return "Do you need an extra bed?"
    if step == "ask_contact":
        return t("ask_contact", lang)
    if step == "pay":
        return t("pay_nudge", lang)
    return ""


RECENT = 3  # a TOOL text (nearly) the same as one of our last 3 messages is dropped (room labels, glimpse ...)
LINK_KEYS = ("pay", "hold_pay")
# the AI's reply, an outage notice, the last fallback and the big-group answer (the AI adds nothing to it) always go
ALWAYS = ("ai", "ai_down", "say_again_short", "group_too_big", "fallback")


def drop_recent(bot: Any, phone: str, outbox: Outbox, *, link_asked: bool = False) -> list[Any]:
    """A queued TOOL text that is (nearly) the same as one of the last RECENT messages we sent is dropped. The AI's own
    reply is never dropped here. The payment link may go again when the guest asked for it (send_payment_link ran)."""
    recent = pp.recent_texts(bot, phone, RECENT)
    gone = [i for i in outbox.queue if i.kind == "text" and i.key not in ALWAYS
            and not (link_asked and i.key in LINK_KEYS) and any(near_same(i.text, x) for x in recent)]
    if gone:
        outbox.queue = [i for i in outbox.queue if i not in gone]
        log.info("Outbox: %s already among the last %d messages to %s; dropped", [i.key for i in gone], RECENT,
                 phone[-4:])
    return gone


def _alert_ai_down(bot: Any, outbox: Outbox, phone: str, text: str, enq: Enquiry) -> None:
    """One alert for the team per outage (30 minutes), through the outbox. A dry run never uses up the slot."""
    now = bot.clock.now()
    last = getattr(bot, "_down_alerted", None)
    if last is not None and now - last < AI_DOWN_ALERT_GAP:
        return
    if not outbox.dry_run:
        bot._down_alerted = now
    outbox.staff("staff:ai_down", f"Bot could not answer a message from {phone}: {text[:200]} (AI unavailable; further "
                 "failures in the next 30 min are not alerted)", bot.staff_numbers(enq.place))
