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
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.providers.llm import ChatMessage
from app.resort.agent import conversation, guard
from app.resort.agent import language as turn_language
from app.resort.agent import photos_plan as pp
from app.resort.agent import usage
from app.resort.agent.outbox import Outbox, near_same
from app.resort.agent.prompt import build_system_prompt
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import I18N_LANG, TOOL_SCHEMAS, AgentTools
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
    """A line for a swipe-reply: which earlier message the guest answered ('' when it is not one of ours to find)."""
    quoted = bot.repo.chat_by_wamid(phone, context_id or "")
    if quoted is None:
        return ""
    if quoted.direction == "out":
        if room := bot._room_of_message(quoted.text):
            rt, number = room
            label = f"Room {number} — {rt.name}" if number else rt.name
            what = "the photo of" if quoted.text.startswith("[photo]") else "the label of"
            return f"[Guest swiped-replied to {what} {label}]"
        return f'[Guest swiped-replied to your message: "{quoted.text[:300]}"]'
    return f'[Guest swiped-replied to their own earlier message: "{quoted.text[:300]}"]'


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
    enq = Enquiry.load(bot.repo, phone)
    messages = _with_guest_message(history(bot, phone), text, swipe)
    fresh = len(messages) <= 1 and not greeted(bot, phone)
    system = build_system_prompt(bot, phone, enq, fresh=fresh, lock=words.lock)
    tools = AgentTools(bot, phone, outbox, guest_text=text, lang=words)
    result = TurnResult("answered")
    flagged: list[str] = []  # a reply() the guard sent back for its one retry (sent trimmed if no retry comes)
    latest: list[tuple[ChatMessage, str]] = []  # the last round's tool results (full) and their one-line summaries

    def complete() -> Any:
        response = llm.complete(system, messages, TOOL_SCHEMAS, MAX_REPLY_TOKENS, temperature=TEMPERATURE)
        result.add_usage(response)
        return response

    def note(response: Any) -> None:
        if raw := _raw(response):  # the AI's own text outside reply(): a private note, never sent
            result.notes.append(raw)
            log.info("Agent note for %s (not sent): %s", phone[-4:], raw[:300])

    def sources() -> guard.Sources:
        return guard.sources(bot, phone, Enquiry.load(bot.repo, phone), result.calls, outbox.texts, text,
                             conv["started_at"])

    def send(reply: str) -> None:
        outbox.text("ai", reply)
        result.reply = (result.reply + "\n" + reply).strip()

    def do_reply(args: dict[str, Any]) -> dict[str, Any]:
        """reply(text): the guest's message, through the guards (a, b: one retry; c: stripped)."""
        if tools.lang is not None:
            tools.lang.take(args.get("language"))  # the reply's language is this turn's language
        src = sources()
        reply = guard.clean(str(args.get("text") or "").strip(), src)
        if not reply:
            return {"ok": False, "facts": {"problem": "the text is empty"}, "note": "Call reply() with your answer."}
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
            if not reply:
                return {"ok": False, "facts": {"problem": found}, "note": "Nothing was sent."}
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
    if failure is None and not outbox.spoke:  # truly empty twice: the one fixed fallback, logged with the reason
        result.outcome = "say_again"
        log.warning("Agent: 'say again' for %s: the AI sent nothing twice (notes: %s)", phone[-4:],
                    " | ".join(result.notes)[:300] or "none")
        outbox.text("say_again_short", t("say_again_short", lang))
    outbox.flush()
    result.sent = outbox.keys
    usage.record(bot, phone, result, dry_run=outbox.dry_run)
    return result


RECENT = 3  # a TOOL text (nearly) the same as one of our last 3 messages is dropped (room labels, glimpse ...)
LINK_KEYS = ("pay", "hold_pay")
ALWAYS = ("ai", "ai_down", "say_again_short")  # the AI's reply, an outage notice and the last fallback always go


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
