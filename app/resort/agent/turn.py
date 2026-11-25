"""One guest turn (docs/AI_AGENT_DESIGN.md sections 1 and 4): the AI reads the message, calls tools, and answers.

    system prompt (cached static part + per-turn part) + the last 12 real messages + the new guest message (with a
    swipe-reply line before it)
      -> LLM <-> tools, at most 4 rounds of tool calls; tool results go back as compact JSON, and an earlier round's
         results shrink to one line each when a new round starts
      -> the outbox: the AI's own line first, then the tools' messages in the order they were queued,
         then the open step if nothing at all went out (never silent)

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
from app.resort.agent import photos_plan as pp
from app.resort.agent import usage
from app.resort.agent.outbox import Outbox, near_same
from app.resort.agent.prompt import build_system_prompt
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import I18N_LANG, STEP_SHOWN, TOOL_SCHEMAS, AgentTools, live_link, pay_text
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
    conversation.remember(bot, phone)  # where this guest's conversation began (restart / confirmed booking / 12 h gap)
    enq = Enquiry.load(bot.repo, phone)
    messages = _with_guest_message(history(bot, phone), text, swipe)
    fresh = len(messages) <= 1 and not greeted(bot, phone)
    system = build_system_prompt(bot, phone, enq, fresh=fresh)
    tools = AgentTools(bot, phone, outbox, guest_text=text)
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

    def polish(reply: str, src: Any) -> tuple[str, bool]:
        """The AI's own line through the quiet guards; (line, the AI asked for booking details itself)."""
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
                reply = guard.drop_sir(reply)  # live test: 'sir' / 'madam' only in the first reply of a chat
        if reply and guard.is_repeat(bot, phone, reply) and not guard.LINK.search(reply):
            log.info("Guard: the AI's line was already sent to %s; dropped", phone[-4:])  # e)
            reply = ""
        return reply, asked

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
            return polish(line, src2)[0]
        except Exception as exc:  # the AI failing here: the code's fallback still answers
            log.warning("Second chance failed for %s: %s", phone[-4:], exc)
            return ""

    reply, src, blocked, booking_dropped = "", None, False, False
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
                reply, booking_dropped = "", True
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
        reply, asked = polish(reply, src)
        result.reply = reply
        if reply:  # design section 4: the AI's own line first, then the tools' messages in their order
            outbox.prepend_text("ai", reply)
        if outbox.spoke and not tools.no_step and (step := due_step(bot, phone, enq, lang, outbox,
                                                                    fresh=fresh and not result.calls, asked=asked)):
            outbox.text(*step)  # the open booking step always closes the turn (fix round 1)
        if not outbox.spoke:  # NEVER SILENT (live test 2026-11-25): every guest message gets at least one reply
            result.outcome = "never_silent"
            wanted = asked or booking_dropped  # the AI's own booking ask / answer was taken out: the step IS wanted
            if step := quiet_reply(bot, phone, enq, lang, wanted=wanted, ack=result.ack):  # pay nudge / step / 'Sure.'
                outbox.text(*step)
            else:
                second = second_chance()  # a) the AI once more (internal: the guest never sees the ask)
                if second:
                    result.reply = second
                    outbox.prepend_text("ai", second)
                if not outbox.spoke:  # b) still nothing: the open step again (a repeat is allowed), else say again
                    enq = Enquiry.load(bot.repo, phone)
                    outbox.text(*(open_step_now(bot, phone, enq, lang, wanted=wanted)
                                  or ("say_again_short", t("say_again_short", lang))))
    outbox.dedupe()  # never the same (or nearly the same) text twice in one turn
    outbox.move_last(STEP_TEXT_KEYS)  # the open step (name ask, form, link) is always the LAST message of the turn
    outbox.flush()
    result.sent = outbox.keys
    usage.record(bot, phone, result, dry_run=outbox.dry_run)
    return result


# the owner texts that ARE each open step: when one went out this turn, the step is not added again
STEP_KEYS = {"ask_location": ("ask_location",), "ask_details": ("form", "past_date"),
             "ask_extra_bed": ("ask_extra_bed", "ask_extra_beds"), "ask_contact": ("ask_contact",),
             "pay": ("pay", "hold_pay")}
# the step texts that always close a turn (moved after the photos / labels / price: live test 2026-11-25)
STEP_TEXT_KEYS = ("ask_location", "form", "ask_extra_bed", "ask_extra_beds", "ask_contact", "pay", "hold_pay",
                  "pay_nudge")
SECOND_CHANCE = ("[SYSTEM] You sent nothing and the guest is waiting. Reply to the guest's last message in one short "
                 "sentence. Call a tool ONLY if their message clearly needs one (e.g. they gave details or asked for "
                 "photos/link).")


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
    tool_spoke = any(i.kind == "photos" or (i.kind == "text" and i.key != "ai") for i in (*outbox.sent, *outbox.queue))
    if not tool_spoke and _recent(bot, phone, found[1], live_link(bot, phone) if step == "pay" else None):
        return None  # fix round 5: only the AI spoke ('ok' / 'thanks') and the step is among the last 2 messages
    return found


def _recent(bot: Any, phone: str, text: str, link: Any = None) -> bool:
    """This text (or, for the payment step, any text with the same link) is among the last 2 messages we sent."""
    recent = pp.recent_texts(bot, phone, 2)
    url = getattr(link, "payment_link_url", "") or ""
    return any(near_same(text, x) or (url and url in x) for x in recent)


def quiet_reply(bot: Any, phone: str, enq: Enquiry, lang: str, *, wanted: bool = False,
                ack: bool = False) -> tuple[str, str] | None:
    """Nothing at all went out this turn (the AI wrote nothing, no tool spoke). The code's own short answer, or None
    (then run_turn asks the AI once more, then re-sends the open step / says 'say again': never silent):
    - an open unpaid link: pay_nudge once (not when pay_nudge or the link is among the last 2 messages)
    - an open step not among the last 2 messages: that step
    - NO step open: 'Sure.' ONLY when the AI marked the message as a pure acknowledgement (`ack`: it wrote exactly ACK
      for 'ok' / 'thanks') and 'Sure.' was not the last message; anything else: None, so the AI is asked once more
      (live test 2026-11-25: 'hello' got 'Sure.')
    'Sure.' never answers while a step is open (live test 2026-11-25: 'what sure' / 'send' got 'Sure' or nothing)."""
    recent = pp.recent_texts(bot, phone, 2)
    if (link := live_link(bot, phone)) is not None and enq.next_step == "pay":
        nudge = t("pay_nudge", lang)
        return None if _recent(bot, phone, nudge, link) else ("pay_nudge", nudge)
    # a step is open while a booking is under way, or when this turn's own booking ask / answer was taken out (`wanted`)
    step = pp.open_step(bot, enq, lang) if (enq.active or wanted) else None
    if step is not None:
        return None if _recent(bot, phone, step[1]) else step
    if not ack or not greeted(bot, phone):
        return None
    sure = ("sure", t("sure", lang))
    return None if recent and near_same(recent[-1], sure[1]) else sure


def open_step_now(bot: Any, phone: str, enq: Enquiry, lang: str, *, wanted: bool = False) -> tuple[str, str] | None:
    """The open step as it is now, even if it was just sent (never silent): the live link's pay text, else the form /
    name ask / location question / extra-bed question; None when nothing is open."""
    if (b := live_link(bot, phone)) is not None:
        return "pay", pay_text(b, lang, bot.clock.now())
    return pp.open_step(bot, enq, lang) if (enq.active or wanted) else None


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
