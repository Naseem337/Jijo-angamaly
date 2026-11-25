"""One guest turn (docs/AI_AGENT_DESIGN.md sections 1 and 4): the AI reads the message, calls tools, and answers.

    system prompt + the last 20 real messages + the new guest message (with a swipe-reply line before it)
      -> LLM <-> tools, at most 6 rounds of tool calls
      -> the outbox: the AI's own line first, then the tools' messages in the order they were queued,
         then the open step if nothing at all went out (never silent)

The model comes from the LLM chain (LLM_CHAIN) only: nothing here names one. Temperature is 0.3. The AI's own text goes
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
from app.resort.agent import guard
from app.resort.agent import photos_plan as pp
from app.resort.agent.outbox import Outbox
from app.resort.agent.prompt import build_system_prompt
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import I18N_LANG, STEP_SHOWN, TOOL_SCHEMAS, AgentTools, live_link, pay_text
from app.resort.chat import RESTART_MARK
from app.resort.i18n import t

log = logging.getLogger(__name__)

MAX_ROUNDS = 6  # rounds of tool calls in one turn
MAX_REPLY_TOKENS = 700
HISTORY = 20  # real messages of the current conversation the AI sees
TEMPERATURE = 0.3
AI_DOWN_ALERT_GAP = timedelta(minutes=30)  # one staff alert per outage, not per message
NO_REPLY = "[no reply]"
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


# ------------------------------------------------------------------ what the AI sees


def _mask_links(text: str) -> str:
    """An old payment link must never be copied into a new answer."""
    return "\n".join(" ".join("[link]" if w.startswith("http") else w for w in line.split(" ")) for line in text.split("\n"))


def history(bot: Any, phone: str) -> list[ChatMessage]:
    """The last HISTORY real messages of the current conversation (a restart or a 24 h gap starts a new one). A run of
    photos is one short note; log lines, templates and old links never reach the AI."""
    rows = bot.repo.list_chat(phone, limit=200)
    items: list[tuple[str, str]] = []
    for m in rows[bot._session_start(rows, by_time=True):]:
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
    return any(m.direction == "out" and m.text != RESTART_MARK for m in rows[bot._session_start(rows, by_time=True):])


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


def _own_text(response: Any) -> str:
    reply = (response.text or "").strip() if response is not None else ""
    return "" if reply.lower() == NO_REPLY else reply


def run_turn(bot: Any, llm: Any, phone: str, text: str, outbox: Outbox, *, swipe: str = "") -> TurnResult:
    """Answer one guest message. The outbox is flushed at the end (a dry-run outbox only records)."""
    enq = Enquiry.load(bot.repo, phone)
    messages = _with_guest_message(history(bot, phone), text, swipe)
    fresh = len(messages) <= 1 and not greeted(bot, phone)
    system = build_system_prompt(bot, phone, enq, fresh=fresh)
    tools = AgentTools(bot, phone, outbox, guest_text=text)
    result = TurnResult("answered")
    response = None
    failure: Exception | None = None

    def ask() -> Any:
        """One LLM reply, with every tool call it makes run and fed back (MAX_ROUNDS for the whole turn)."""
        response = llm.complete(system, messages, TOOL_SCHEMAS, MAX_REPLY_TOKENS, temperature=TEMPERATURE)
        while response.tool_calls:
            if result.rounds >= MAX_ROUNDS:
                result.hit_limit = True
                log.warning("Agent turn for %s still wanted tools after %d rounds", phone[-4:], MAX_ROUNDS)
                break
            messages.append(response.to_message())
            for call in response.tool_calls:
                if call.arguments_error:  # the model did not send a JSON object: it is told, the tool does not run
                    out = tools._result(False, {"problem": call.arguments_error}, len(outbox.keys),
                                        Enquiry.load(bot.repo, phone), "Call the tool again with a JSON object.")
                else:
                    out = tools.call(call.name, call.arguments)
                result.calls.append({"name": call.name, "args": call.arguments, "result": out})
                messages.append(ChatMessage.tool_result(call.id, json.dumps(out, ensure_ascii=False), is_error=not out["ok"]))
            result.rounds += 1
            response = llm.complete(system, messages, TOOL_SCHEMAS, MAX_REPLY_TOKENS, temperature=TEMPERATURE)
        return response

    def check(reply: str) -> tuple[list[str], guard.Sources]:
        enq = Enquiry.load(bot.repo, phone)
        src = guard.sources(bot, phone, enq, result.calls, outbox.texts, text)
        return guard.problems(reply, src), src

    def own(response: Any) -> str:
        reply = _own_text(response)
        if tools.quiet:  # talk_to_owner(cannot_answer / call_request / other / guest_called): only the fixed text goes
            return ""
        if reply and outbox.spoke and guard.repeats_tools(reply):  # g) the tools already said it
            log.info("Guard: the AI's line repeats the tools' messages for %s; dropped", phone[-4:])
            return ""
        return reply

    reply, src, blocked = "", None, False
    try:
        if llm is None:
            raise RuntimeError("no LLM configured")
        response = ask()
        reply = own(response)
        found, src = check(reply)
        style = guard.style_problems(reply)  # too long / a banned phrase (fix round 3)
        if found or style:  # design section 6: ONE retry, the AI is told exactly what was wrong
            result.guard, result.retried = found + style, True
            log.info("Guard: retry for %s: %s", phone[-4:], found + style)
            messages.append(ChatMessage("assistant", reply))
            messages.append(ChatMessage("user", guard.retry_message(found + style)))
            response = ask()
            reply = own(response)
            found, src = check(reply)
            if found:
                blocked, reply = True, ""
                result.guard = result.guard + found
            elif guard.style_problems(reply):  # still too long or banned: cut, never a handoff
                reply = guard.fix_style(reply)
    except Exception as exc:  # an LLM outage or a crash must never leave the guest unanswered
        failure = exc
        log.warning("AI turn failed: %s", exc)

    enq = Enquiry.load(bot.repo, phone)  # the tools may have changed it
    lang = I18N_LANG[enq.language]
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
        if reply and src is not None:
            reply = guard.clean(reply, src)  # d) provider name, booking ids, other scripts, old links, the name
        asked = False  # the AI asked for booking details itself: the code's open step takes its place
        if reply:
            kept = guard.drop_detail_asks(reply)  # f) the code's open step asks for the details, not the AI
            asked, reply = kept != reply, kept
        if reply:  # h) i) j) fix round 2
            reply = guard.drop_hold_talk(reply)  # never explain the hold or when the link expires
            if not tools.sending_ran:
                reply = guard.drop_false_sent(reply)  # "sent again" / "informed the team" with nothing sent
            if not fresh:
                reply = guard.mid_chat_greeting(reply)  # "How may I assist you?" only as the first greeting
        if reply and guard.is_repeat(bot, phone, reply) and not guard.LINK.search(reply):
            log.info("Guard: the AI's line was already sent to %s; dropped", phone[-4:])  # e)
            reply = ""
        result.reply = reply
        if reply:  # design section 4: the AI's own line first, then the tools' messages in their order
            outbox.prepend_text("ai", reply)
        if outbox.spoke and not tools.no_step and (step := due_step(bot, phone, enq, lang, outbox,
                                                                    fresh=fresh and not result.calls, asked=asked)):
            outbox.text(*step)  # the open booking step always closes the turn (fix round 1)
        if not outbox.spoke:  # never silent: the booking step still open, else a short ask to say it again
            step = pp.open_step(bot, enq, lang)
            outbox.text(*(step or ("say_again", t("say_again", lang))))
            result.outcome = "never_silent"
    outbox.dedupe()  # never the same (or nearly the same) text twice in one turn
    outbox.flush()
    result.sent = outbox.keys
    return result


# the owner texts that ARE each open step: when one went out this turn, the step is not added again
STEP_KEYS = {"ask_location": ("ask_location",), "ask_details": ("form", "past_date"),
             "ask_extra_bed": ("ask_extra_bed", "ask_extra_beds"), "ask_contact": ("ask_contact",),
             "pay": ("pay", "hold_pay")}


def due_step(bot: Any, phone: str, enq: Enquiry, lang: str, outbox: Outbox, *, fresh: bool = False,
             asked: bool = False) -> tuple[str, str] | None:
    """The open booking step the code adds at the end of a turn: (key, owner text), or None. Only while a booking is
    under way (never on a first 'hi') or when the AI's own ask for details was dropped (`asked`), only when the step
    needs the guest (location, details, extra bed, name and mobile, payment), when its owner text was not sent this
    turn and it is not already the last message we sent."""
    step = enq.next_step
    if step not in STEP_KEYS or any(k in outbox.keys for k in (*STEP_KEYS[step], STEP_SHOWN)):
        return None
    if not asked and (fresh or not enq.active):
        return None
    if step == "pay":
        b = live_link(bot, phone)
        found = ("pay", pay_text(b, lang, bot.clock.now())) if b is not None else None
    elif step == "ask_location" and len([p for p in bot._places() if p.active]) == 1:
        found = None  # one resort only: the place is never asked
    else:
        found = pp.open_step(bot, enq, lang)
    if found is None or found[1] == pp.last_text(bot, phone, outbox):
        return None
    return found


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
