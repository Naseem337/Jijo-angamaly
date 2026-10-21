"""One customer turn: message in → tools → guardrails → reply out.

Agent.handle() owns the whole turn so the WhatsApp router (step 6) and the terminal chat
(app.cli_chat) behave identically: it stores the inbound message, runs the LLM tool loop,
checks the reply, stores the outbound message and returns an AgentReply. Opt-outs,
staff commands and paused chats are handled by the router before the agent is called.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.agent import prompts
from app.agent.confirmation import resolve_pending
from app.agent.guardrails import SourcedFacts, Violation, check_reply, facts_from
from app.agent.spoken import invented_words, draft_spoken, to_speech
from app.agent.tools import TOOL_SPECS, HandoffRequest, ToolContext, run_tool
from app.core.client_config import ClientConfig
from app.core.clock import Clock, ist_today
from app.core.language import asks_for_voice, detect_language, is_language_neutral, reply_language_problem
from app.inventory import InventoryAdapter
from app.providers.llm import ChatMessage, LLMError, LLMProvider, LLMResponse
from app.storage.models import Message
from app.storage.repository import Repository

log = logging.getLogger(__name__)

_ODD_DASHES = str.maketrans({c: "-" for c in "‐‑‒−"})


_GREETING = r"(hi|hello|hey|dear|namaskaram|thanks|thank you|welcome)"


def without_names(text: str, names: list[str | None]) -> str:
    """Remove the customer's name where a reply addresses them ('Hi Danish,' -> 'Hi,',
    'Danish, ...' -> '...', 'Thanks, Danish!' -> 'Thanks!'). Only address positions are touched,
    so a profile name that is also a normal word ('Swift') can't damage car talk."""
    words = sorted({w for n in names if n for w in re.findall(r"[A-Za-z]{3,}", n)}, key=len, reverse=True)
    if not words:
        return text
    one = "|".join(re.escape(w) for w in words)
    name = rf"(?:{one})(?:\s+(?:{one}))*"
    out = re.sub(rf"\b{_GREETING}\s+{name}\b", r"\1", text, flags=re.IGNORECASE)
    out = re.sub(rf"(?m)^([ \t]*){name}[ \t]*[,!][ \t]*", r"\1", out, flags=re.IGNORECASE)
    out = re.sub(rf",[ \t]*{name}\b(?=[ \t]*(?:[!?.,]|$))", "", out, flags=re.IGNORECASE | re.MULTILINE)
    return out


_TITLES = re.compile(r"[ \t]*(?<!\w)(?:sir|madam|ma'am|maam|സാർ|സർ|മാഡം)(?!\w)", re.IGNORECASE)


def without_titles(text: str) -> str:
    """Drop sir / madam (English and Malayalam): the bot never guesses gender.
    'Undu sir, 5 lakh' -> 'Undu, 5 lakh'; 'സാർ, വില...' -> 'വില...'; 'Hello sir!' -> 'Hello!'."""
    out = _TITLES.sub("", text)
    out = re.sub(r"(?m)^([ \t]*)[,!][ \t]*", r"\1", out)  # a line that started with the title
    out = re.sub(r",[ \t]*([,.!?])", r"\1", out)
    return out


_PLACEHOLDER = re.compile(r"\[[^\]\n]{2,40}\]|\{\{?[^}\n]+\}\}?|<[A-Za-z ]{3,30}>")


def placeholder_problem(text: str) -> str | None:
    """'[insert address]', '{name}', '<link>': a template slot the AI left unfilled."""
    if _PLACEHOLDER.search(text):
        return ("The reply contains a placeholder like [insert address]. Never write placeholders: "
                "if you don't have the fact, say the team will share it.")
    return None


def plain_dashes(text: str) -> str:
    """Some models write ids with a non-breaking hyphen ('TC‑2253AD'); car ids must use '-'
    so customers can copy them and the router can find car lines. Range dashes (–, —) stay."""
    return text.translate(_ODD_DASHES)

MAX_TOOL_ROUNDS = 5
HISTORY_CHARS = 300
DATA_TOOLS = frozenset({"search_cars", "get_car_details", "emi_estimate"})
MAX_REPLY_TOKENS = 700


@dataclass
class AgentReply:
    text: str
    language: str
    handoff: HandoffRequest | None = None
    tools_used: list[str] = field(default_factory=list)
    guardrail: str = "ok"  # ok | rewritten | fallback | llm_error
    problems: list[str] = field(default_factory=list)
    provider: str = ""
    saved: list[str] = field(default_factory=list)
    spoken_text: str | None = None  # set when a voice reply was requested and passed the checks
    usage: dict[str, int] = field(default_factory=dict)  # AI calls and tokens spent on this turn
    photos: list[str] = field(default_factory=list)  # car ids: send their main photo after the reply

    @property
    def has_car_details(self) -> bool:
        """The reply carries stock data (cars, prices, EMI): it must also go out as text."""
        return bool(set(self.tools_used) & DATA_TOOLS)


class _Meter:
    """Wraps the LLM for one customer turn and adds up what every call cost."""

    def __init__(self, llm: LLMProvider) -> None:
        self.llm = llm
        self.name = getattr(llm, "name", "llm")
        self.totals = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0}

    def complete(self, system, messages, tools=None, max_tokens=1024) -> LLMResponse:
        response = self.llm.complete(system, messages, tools, max_tokens)
        self.totals["calls"] += 1
        self.totals["tokens_in"] += response.usage.input_tokens
        self.totals["tokens_out"] += response.usage.output_tokens
        self.totals["tokens_cached"] += response.usage.cached_input_tokens
        return response


TOKEN_COUNTERS = ("calls", "tokens_in", "tokens_out", "tokens_cached")


class Agent:
    def __init__(self, config: ClientConfig, llm: LLMProvider, repo: Repository,
                 inventory: InventoryAdapter, clock: Clock) -> None:
        self.config = config
        self.llm = llm
        self.repo = repo
        self.inventory = inventory
        self.clock = clock
        self.system_prompt = prompts.build_system_prompt(config)

    # ------------------------------------------------------------------ public

    def handle(self, phone: str, text: str, *, source: str = "text",
               message_id: str | None = None, want_voice: bool = False,
               profile_name: str | None = None) -> AgentReply:
        now = self.clock.now()
        cid = self.config.client_id
        customer = self.repo.get_or_create_customer(cid, phone, now)
        history = self.repo.list_messages(cid, phone, limit=self.config.business_rules.bot.history_messages)

        language = detect_language(text, default=customer.language)  # type: ignore[arg-type]
        if language != customer.language:
            customer.language = language
            self.repo.save_customer(customer)
        inbound = Message(cid, phone, "in", text, now, source="voice" if source == "voice" else "text")
        if message_id:
            inbound.id = message_id
        self.repo.add_message(inbound)
        self.repo.reset_unanswered(cid, phone, now)

        ctx = ToolContext(self.config, self.repo, self.inventory, phone, now, language, source)
        # A new customer's "Hi" doesn't show their language yet, so the reply language is only
        # enforced once this message or an earlier one does.
        ctx.enforce_language = not is_language_neutral(text) or any(m.direction == "in" for m in history)
        notes = [n for n in [resolve_pending(ctx, text)] if n]
        if want_voice and source != "voice" and asks_for_voice(text):
            notes.append("The customer asked for a voice reply: your reply WILL be sent as a voice note. Give "
                         "the answer they were waiting for (e.g. explain or compare the cars just discussed) "
                         "in the reply; don't ask what they mean and don't mention voice or audio.")
        context = prompts.turn_context(self.config, language=language, now=now,
                                       customer_name=customer.name, source=source, notes=notes)
        history = [m for m in history if not m.meta.get("voice_note")]  # voice copies of text replies
        # Old replies are cut to HISTORY_CHARS: a car list from 6 messages ago costs tokens on every
        # call but rarely matters (the agent can search again; guardrails still see the full text).
        messages = [ChatMessage("user" if m.direction == "in" else "assistant",
                                m.text if len(m.text) <= HISTORY_CHARS else m.text[:HISTORY_CHARS] + "…")
                    for m in history]
        if messages and messages[0].role == "assistant":  # we wrote first, e.g. a car alert: keep it
            messages.insert(0, ChatMessage.user("(earlier conversation)"))
        messages.append(ChatMessage.user(f"{context}\n{text}"))
        facts = facts_from(texts=[m.text for m in history] + [text] + notes,
                           years=[now.year, now.year - 1])

        meter = _Meter(self.llm)
        reply = self._run(messages, ctx, facts, language, meter)
        # User rules (2026-10-21): never the customer's name, never sir/madam (no gender guessing).
        reply.text = without_titles(without_names(reply.text, [customer.name, profile_name]))
        reply.saved = list(ctx.saved)
        reply.photos = list(ctx.photos) if reply.guardrail in ("ok", "rewritten") else []
        if want_voice:
            spoken = self._spoken(reply, facts, meter)
            reply.spoken_text = without_titles(spoken) if spoken else spoken
        reply.usage = dict(meter.totals)
        day = ist_today(self.clock).isoformat()
        for name in TOKEN_COUNTERS:  # daily totals: python -m app.usage --client <id>
            if reply.usage[name]:
                self.repo.increment_counter(cid, f"llm_{name}", day, by=reply.usage[name])
        self.repo.add_message(Message(cid, phone, "out", reply.text, self.clock.now(), meta={
            "provider": reply.provider, "tools": reply.tools_used, "guardrail": reply.guardrail,
            "problems": reply.problems, "handoff": reply.handoff.reason if reply.handoff else None,
            "spoken": reply.spoken_text is not None, "usage": reply.usage,
        }))
        return reply

    # ------------------------------------------------------------------ internals

    def _spoken(self, reply: AgentReply, facts: SourcedFacts, llm: LLMProvider) -> str | None:
        """Voice-note script for the reply, or None to send text only.

        Apologies (AI down, "let me check with the team") stay text only: speaking them
        adds cost and nothing for the customer (user decision, 2026-10-19)."""
        if reply.guardrail in ("fallback", "llm_error"):
            return None
        draft = None
        for _ in range(2):  # a script that invents a car or fact gets one more try
            draft = draft_spoken(llm, reply.text, reply.language, self.config.business_rules.speaking_style,
                                 details_in_text=reply.has_car_details)
            if not draft:
                return None
            invented = invented_words(draft, reply.text)
            if not invented:
                break
            log.warning("Voice script invented words %s; %s", invented, "retrying" if _ == 0 else "text only")
            draft = None
        if not draft:
            return None
        problems = check_reply(draft, facts)
        if problems:
            log.warning("Spoken version failed guardrails, sending text only: %s", problems)
            return None
        return to_speech(draft, reply.language) or None

    def _run(self, messages: list[ChatMessage], ctx: ToolContext, facts: SourcedFacts,
             language: str, llm: LLMProvider) -> AgentReply:
        tools_used: list[str] = []

        def complete() -> LLMResponse:
            return llm.complete(self.system_prompt, messages, TOOL_SPECS, MAX_REPLY_TOKENS)

        try:
            response = complete()
            for _ in range(MAX_TOOL_ROUNDS):
                if not response.tool_calls:
                    break
                messages.append(response.to_message())
                for call in response.tool_calls:
                    result = run_tool(ctx, call)
                    tools_used.append(call.name)
                    facts.add_json(result.content)
                    messages.append(ChatMessage.tool_result(call.id, result.content, is_error=result.is_error))
                response = complete()
        except LLMError as exc:
            log.error("LLM unavailable for %s: %s", ctx.phone, exc)
            ctx.handoff = ctx.handoff or HandoffRequest("llm_unavailable", "The bot could not answer (AI service down).")
            return AgentReply(prompts.TECHNICAL_ISSUE_REPLY[language], language, ctx.handoff,
                              tools_used, "llm_error", [str(exc)])

        text = plain_dashes(response.text.strip())
        if response.tool_calls or response.stop_reason == "refusal" or not text:
            reason = "too many tool rounds" if response.tool_calls else response.stop_reason
            return self._fallback(ctx, language, tools_used, [f"no usable reply ({reason})"], response.provider)

        problems = check_reply(text, facts)
        wrong_language = (reply_language_problem(text, language) if ctx.enforce_language else None)             or placeholder_problem(text)
        if not problems and not wrong_language:
            return AgentReply(text, language, ctx.handoff, tools_used, "ok", provider=response.provider)
        if not problems:  # only the language is off: one rewrite, else send it as it is (never a fallback)
            log.info("Language rewrite for %s: %s", ctx.phone, wrong_language)
            rewritten = self._rewrite(messages, response, [wrong_language], llm)
            if (rewritten and not check_reply(rewritten.text, facts)
                    and not reply_language_problem(rewritten.text, language)):
                return AgentReply(plain_dashes(rewritten.text.strip()), language, ctx.handoff, tools_used,
                                  "rewritten", [wrong_language], rewritten.provider)
            return AgentReply(text, language, ctx.handoff, tools_used, "ok", [wrong_language], response.provider)
        if wrong_language:
            problems = [*problems, wrong_language]

        log.warning("Guardrail rewrite for %s: %s", ctx.phone, problems)
        rewritten = self._rewrite(messages, response, problems, llm)
        if rewritten and not check_reply(rewritten.text, facts):
            return AgentReply(plain_dashes(rewritten.text.strip()), language, ctx.handoff, tools_used, "rewritten",
                              [str(p) for p in problems], rewritten.provider)
        return self._fallback(ctx, language, tools_used, [str(p) for p in problems], response.provider)

    def _rewrite(self, messages: list[ChatMessage], draft: LLMResponse,
                 problems: list[Violation], llm: LLMProvider) -> LLMResponse | None:
        instruction = prompts.REWRITE_INSTRUCTION.format(problems="; ".join(str(p) for p in problems))
        try:
            # No tools on the rewrite: it must only rephrase what it already knows.
            response = llm.complete(self.system_prompt,
                                         [*messages, draft.to_message(), ChatMessage.user(instruction)],
                                         None, MAX_REPLY_TOKENS)
        except LLMError as exc:
            log.warning("Guardrail rewrite failed: %s", exc)
            return None
        return response if response.text.strip() and not response.tool_calls else None

    def _fallback(self, ctx: ToolContext, language: str, tools_used: list[str], problems: list[str],
                  provider: str) -> AgentReply:
        ctx.handoff = ctx.handoff or HandoffRequest("guardrail", "Bot could not give a verified answer; please follow up.")
        return AgentReply(prompts.FALLBACK_REPLY[language], language, ctx.handoff, tools_used,
                          "fallback", problems, provider)
