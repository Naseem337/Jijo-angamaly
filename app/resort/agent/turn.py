"""One guest turn (docs/AI_AGENT_DESIGN.md sections 1 and 4): the AI reads the message, calls tools, and answers.

    system prompt (cached static part + per-turn part) + the last 12 real messages + the new guest message (with a
    swipe-reply line before it)
      -> LLM <-> tools, at most 4 rounds of tool calls; tool results go back as compact JSON, and an earlier round's
         results shrink to one line each when a new round starts
      -> the outbox: the AI's own line first, then the tools' messages in the order they were queued; a text that is
         among our last 3 messages is dropped (owner texts too; only a link the guest asked for may go again)
      -> never silent: nothing left -> the AI once more -> "Sorry, could you say that again?". A step's owner text
         goes only when that step starts or changes (a tool sends it); after that the AI reminds in its own words.

The model comes from the LLM chain (LLM_CHAIN) only: nothing here names one. Temperature 0.2, at most 300 output tokens
per call, no extended thinking. Token use per turn goes to usage.py. The AI's own text goes
through guard.py (one retry with the problem told to the AI; still bad -> handoff text + staff alert); tool messages are
never checked. A failing LLM gets the ai_down text and one staff alert per 30 minutes.
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
MAX_REPLY_TOKENS = 300  # the AI's line is one short sentence; tool calls are small
HISTORY = 12  # real messages of the current conversation the AI sees (CURRENT ENQUIRY already holds the stay)
TEMPERATURE = 0.2
AI_DOWN_ALERT_GAP = timedelta(minutes=30)  # one staff alert per outage, not per message
NO_REPLY = "[no reply]"
ACK = "[ack]"  # the AI's whole reply for a pure acknowledgement ('ok', 'thanks'): only then may the code say 'Sure.'
SKIPPED_ROWS = ("[invoice]", "[template", "[image]", "[voice note: not understood")  # log lines, never shown to the AI


@dataclass
class TurnResult:
    outcome: str  # answered | never_silent | ai_down
    reply: str = ""  # the AI's own line
    rounds: int = 0  # rounds of tool calls that ran
    hit_limit: bool = False  # the AI still wanted tools after MAX_ROUNDS
    calls: list[dict[str, Any]] = field(default_factory=list)  # [{"name", "args", "result"}] in order
    sent: list[str] = field(default_factory=list)  # the outbox keys, in the order they went out
    error: str = ""
    guard: list[str] = field(default_factory=list)  # what the guard found wrong with the AI's own text
    retried: bool = False  # the AI got its one retry
    ack: bool = False  # the AI's reply was exactly ACK: the guest only acknowledged ('ok', 'thanks')
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


# ------------------------------------------------------------------ the turn


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
    ask_contact, next ask_contact'. The latest round's results stay in full."""
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


def is_ack(response: Any) -> bool:
    """The AI marked the guest's message as a pure acknowledgement ('ok', 'thanks') by writing exactly ACK. This reads the
    AI's own output, never the guest's words."""
    return _raw(response).lower() == ACK


def _own_text(response: Any) -> str:
    reply = _raw(response)
    return "" if reply.lower() in (NO_REPLY, ACK) else reply


def run_turn(bot: Any, llm: Any, phone: str, text: str, outbox: Outbox, *, swipe: str = "") -> TurnResult:
    """Answer one guest message. The outbox is flushed at the end (a dry-run outbox only records)."""
    conv = conversation.remember(bot, phone)  # where this conversation began (restart / confirmed booking / 12 h gap)
    words = turn_language.load(bot, phone, conv["started_at"])  # never a language from another conversation
    enq = Enquiry.load(bot.repo, phone)
    messages = _with_guest_message(history(bot, phone), text, swipe)
    fresh = len(messages) <= 1 and not greeted(bot, phone)
    system = build_system_prompt(bot, phone, enq, fresh=fresh, lock=words.lock)
    tools = AgentTools(bot, phone, outbox, guest_text=text, lang=words)

    def recent_rule() -> None:  # never a text among our last 3 messages; a link the guest asked for may go again
        drop_recent(bot, phone, outbox, link_asked=any(n in tools.ran for n in ("send_payment_link", "repeat_step")))
    result = TurnResult("answered")
    response = None
    failure: Exception | None = None

    latest: list[tuple[ChatMessage, str]] = []  # the last round's tool results (full) and their one-line summaries

    def complete() -> Any:
        response = llm.complete(system, messages, TOOL_SCHEMAS, MAX_REPLY_TOKENS, temperature=TEMPERATURE)
        result.add_usage(response)
        return response

    def ask() -> Any:
        """One LLM reply, with every tool call it makes run and fed back (MAX_ROUNDS for the whole turn)."""
        response = complete()
        while response.tool_calls:
            if result.rounds >= MAX_ROUNDS:
                result.hit_limit = True
                log.warning("Agent turn for %s still wanted tools after %d rounds", phone[-4:], MAX_ROUNDS)
                break
            for msg, short in latest:  # an older round's results: one line each (the newest round stays in full)
                msg.content = short
            latest.clear()
            messages.append(response.to_message())
            for call in response.tool_calls:
                if call.arguments_error:  # the model did not send a JSON object: it is told, the tool does not run
                    out = tools._result(False, {"problem": call.arguments_error}, len(outbox.keys),
                                        Enquiry.load(bot.repo, phone), "Call the tool again with a JSON object.")
                else:
                    out = tools.call(call.name, call.arguments)
                result.calls.append({"name": call.name, "args": call.arguments, "result": out})
                msg = ChatMessage.tool_result(call.id, compact_result(out), is_error=not out["ok"])
                messages.append(msg)
                latest.append((msg, summary_line(call.name, out)))
            result.rounds += 1
            response = complete()
        return response

    def check(reply: str) -> tuple[list[str], guard.Sources]:
        enq = Enquiry.load(bot.repo, phone)
        src = guard.sources(bot, phone, enq, result.calls, outbox.texts, text)
        return guard.problems(reply, src), src

    def own(response: Any) -> str:
        recent_rule()  # a tool text that may not go (among our last 3) never pushes the AI's own line out
        result.ack = is_ack(response)
        reply = _own_text(response)
        if tools.quiet:  # talk_to_owner(cannot_answer / call_request / other / guest_called): only the fixed text goes
            return ""
        if reply and outbox.spoke and (guard.repeats_tools(reply) or guard.restates_tools(reply, outbox.texts)):
            # g) the tools already said it; fix round 5: a line that only talks about what the tools sent goes too
            log.info("Guard: the AI's line repeats the tools' messages for %s; dropped", phone[-4:])
            return ""
        return reply

    names = guard.place_names(bot)

    def booking(reply: str) -> list[str]:
        """Fix round 5: the AI answered booking information itself (a resort / town with rooms or availability, 'we
        have properties', which place?) and update_enquiry did not run: it must call the tool instead."""
        if reply and "update_enquiry" not in tools.ran and not tools.sending_ran and guard.booking_talk(reply, names):
            return [guard.BOOKING_RETRY]
        return []

    def polish(reply: str, src: Any) -> str:
        """The AI's own line through the quiet guards. Its own reminder of an open step ('Please send your name and
        mobile number to continue.') stays: since the AI-first rebalance (2026-11-25) the code never re-sends the
        owner's step text."""
        if reply and src is not None:
            reply = guard.clean(reply, src)  # d) provider name, booking ids, other scripts, old links, the name
        if reply:  # h) i) j) fix round 2
            reply = guard.drop_hold_talk(reply)  # never explain the hold or when the link expires
            if not tools.sending_ran:
                reply = guard.drop_false_sent(reply)  # "sent again" / "informed the team" with nothing sent
            if not fresh:
                reply = guard.mid_chat_greeting(reply)  # "How may I assist you?" only as the first greeting
                reply = guard.drop_sir(reply)  # live test: 'sir' / 'madam' only in the first reply of a chat
        if reply and guard.is_repeat(bot, phone, reply) and not guard.LINK.search(reply):
            log.info("Guard: the AI's line was already sent to %s; dropped", phone[-4:])  # e)
            reply = ""
        return reply

    def second_chance() -> str:
        """Nothing would go out: the AI is asked once more (never shown to the guest). Its tools run as usual; its line
        passes every guard (a line that still fails is dropped, never retried)."""
        if llm is None or failure is not None:
            return ""
        try:
            if messages and messages[-1].role == "user":
                messages[-1].content += "\n\n" + SECOND_CHANCE
            else:
                messages.append(ChatMessage("user", SECOND_CHANCE))
            line = own(ask())
            found, src2 = check(line)
            if found or booking(line):
                return ""
            if guard.style_problems(line):
                line = guard.fix_style(line)
            return polish(line, src2)
        except Exception as exc:  # the AI failing here: the code's fallback still answers
            log.warning("Second chance failed for %s: %s", phone[-4:], exc)
            return ""

    reply, src, blocked = "", None, False
    try:
        if llm is None:
            raise RuntimeError("no LLM configured")
        response = ask()
        reply = own(response)
        found, src = check(reply)
        style = guard.style_problems(reply)  # too long / a banned phrase (fix round 3)
        wrong = found + style + booking(reply)
        if wrong:  # design section 6: ONE retry, the AI is told exactly what was wrong
            result.guard, result.retried = wrong, True
            log.info("Guard: retry for %s: %s", phone[-4:], wrong)
            messages.append(ChatMessage("assistant", reply))
            messages.append(ChatMessage("user", guard.retry_message(wrong)))
            response = ask()
            reply = own(response)
            found, src = check(reply)
            if found:
                blocked, reply = True, ""
                result.guard = result.guard + found
            elif booking(reply):  # still answering booking information itself: dropped, the code's open step goes
                log.info("Guard: booking talk without update_enquiry for %s; dropped", phone[-4:])
                reply = ""
            elif guard.style_problems(reply):  # still too long or banned: cut, never a handoff
                reply = guard.fix_style(reply)
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
    else:
        if blocked:  # still wrong after the retry: the owner's handoff text and the team are told
            result.outcome = "handoff"
            log.warning("Guard: reply blocked for %s: %s", phone[-4:], result.guard)
            if "handoff" not in outbox.keys:
                outbox.text("handoff", bot._owner_msg("handoff", {"lang": lang, "form_place": enq.place}))
            outbox.staff("staff:guard", bot.handoff_summary(
                phone, {"lang": lang}, "the bot's reply failed its checks twice: " + " | ".join(result.guard)[:600]),
                bot.staff_numbers(enq.place))
        reply = polish(reply, src)
        result.reply = reply
        if reply:  # design section 4: the AI's own line first, then the tools' messages in their order
            outbox.prepend_text("ai", reply)
    outbox.dedupe()  # never the same (or nearly the same) text twice in one turn

    recent_rule()
    if failure is None and not outbox.spoke:  # NEVER SILENT: every guest message gets an answer
        result.outcome = "never_silent"
        if result.ack:  # the AI marked a pure 'ok' / 'thanks': 'Sure.' (unless it is among our last 3 messages)
            outbox.text("sure", t("sure", lang))
            recent_rule()
        if not outbox.spoke:  # the AI once more (the guest never sees the ask); its tools' texts pass the rule too
            if second := second_chance():
                result.reply = second
                outbox.prepend_text("ai", second)
            outbox.dedupe()
            recent_rule()
        if not outbox.spoke:  # still nothing: 'Sorry, could you say that again?', never a fixed step text again
            outbox.text("say_again_short", t("say_again_short", lang))
    outbox.move_last(STEP_TEXT_KEYS)  # a step text a tool sent this turn (name ask, form, link) goes LAST
    outbox.flush()
    result.sent = outbox.keys
    usage.record(bot, phone, result, dry_run=outbox.dry_run)
    return result


# the step texts that always close a turn (moved after the photos / labels / price: live test 2026-11-25)
STEP_TEXT_KEYS = ("ask_location", "form", "ask_extra_bed", "ask_extra_beds", "ask_contact", "pay", "hold_pay",
                  "pay_nudge")
SECOND_CHANCE = ("[SYSTEM] You sent nothing and the guest is waiting. Reply to the guest's last message in one short "
                 "sentence. Call a tool ONLY if their message clearly needs one (e.g. they gave details or asked for "
                 "photos/link).")


RECENT = 3  # never a text that is (nearly) the same as one of our last 3 messages (AI-first rebalance, 2026-11-25)
LINK_KEYS = ("pay", "hold_pay")
ALWAYS = ("ai_down", "say_again_short")  # an outage notice and the last fallback: the guest is never left silent


def drop_recent(bot: Any, phone: str, outbox: Outbox, *, link_asked: bool = False) -> list[Any]:
    """HARD RULE: a queued text that is (nearly) the same as one of the last RECENT messages we sent is dropped, owner
    texts too. Only the payment link may go again, when the guest asked for it (send_payment_link / repeat_step ran)."""
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
