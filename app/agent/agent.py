"""One customer turn: message in → tools → guardrails → reply out.

Agent.handle() owns the whole turn so the WhatsApp router (step 6) and the terminal chat
(app.cli_chat) behave identically: it stores the inbound message, runs the LLM tool loop,
checks the reply, stores the outbound message and returns an AgentReply. Opt-outs,
staff commands and paused chats are handled by the router before the agent is called.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.agent import prompts
from app.agent.confirmation import resolve_pending
from app.agent.guardrails import SourcedFacts, Violation, check_reply, facts_from
from app.agent.spoken import draft_spoken, speech_language, to_speech
from app.agent.tools import TOOL_SPECS, HandoffRequest, ToolContext, run_tool
from app.core.client_config import ClientConfig
from app.core.clock import Clock
from app.core.language import detect_language
from app.inventory import InventoryAdapter
from app.providers.llm import ChatMessage, LLMError, LLMProvider, LLMResponse
from app.storage.models import Message
from app.storage.repository import Repository

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
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
               message_id: str | None = None, want_voice: bool = False) -> AgentReply:
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
        notes = [n for n in [resolve_pending(ctx, text)] if n]
        context = prompts.turn_context(self.config, language=language, now=now,
                                       customer_name=customer.name, source=source, notes=notes)
        history = [m for m in history if not m.meta.get("voice_note")]  # voice copies of text replies
        messages = [ChatMessage("user" if m.direction == "in" else "assistant", m.text) for m in history]
        while messages and messages[0].role == "assistant":  # e.g. an alert we sent first
            messages.pop(0)
        messages.append(ChatMessage.user(f"{context}\n{text}"))
        facts = facts_from(texts=[m.text for m in history] + [text] + notes,
                           years=[now.year, now.year - 1])

        reply = self._run(messages, ctx, facts, language)
        reply.saved = list(ctx.saved)
        if want_voice:
            reply.spoken_text = self._spoken(reply, facts)
        self.repo.add_message(Message(cid, phone, "out", reply.text, self.clock.now(), meta={
            "provider": reply.provider, "tools": reply.tools_used, "guardrail": reply.guardrail,
            "problems": reply.problems, "handoff": reply.handoff.reason if reply.handoff else None,
            "spoken": reply.spoken_text is not None,
        }))
        return reply

    # ------------------------------------------------------------------ internals

    def _spoken(self, reply: AgentReply, facts: SourcedFacts) -> str | None:
        """Voice-note script for the reply, or None to send text only."""
        if reply.guardrail in ("fallback", "llm_error"):
            # Fixed safe wording; speak it in the voice's script (cacheable, no LLM needed).
            texts = prompts.FALLBACK_REPLY if reply.guardrail == "fallback" else prompts.TECHNICAL_ISSUE_REPLY
            return texts[speech_language(reply.language)]
        draft = draft_spoken(self.llm, reply.text, reply.language, self.config.business_rules.speaking_style)
        if not draft:
            return None
        problems = check_reply(draft, facts)
        if problems:
            log.warning("Spoken version failed guardrails, sending text only: %s", problems)
            return None
        return to_speech(draft, reply.language) or None

    def _complete(self, messages: list[ChatMessage]) -> LLMResponse:
        return self.llm.complete(self.system_prompt, messages, TOOL_SPECS, MAX_REPLY_TOKENS)

    def _run(self, messages: list[ChatMessage], ctx: ToolContext, facts: SourcedFacts,
             language: str) -> AgentReply:
        tools_used: list[str] = []
        try:
            response = self._complete(messages)
            for _ in range(MAX_TOOL_ROUNDS):
                if not response.tool_calls:
                    break
                messages.append(response.to_message())
                for call in response.tool_calls:
                    result = run_tool(ctx, call)
                    tools_used.append(call.name)
                    facts.add_json(result.content)
                    messages.append(ChatMessage.tool_result(call.id, result.content, is_error=result.is_error))
                response = self._complete(messages)
        except LLMError as exc:
            log.error("LLM unavailable for %s: %s", ctx.phone, exc)
            ctx.handoff = ctx.handoff or HandoffRequest("llm_unavailable", "The bot could not answer (AI service down).")
            return AgentReply(prompts.TECHNICAL_ISSUE_REPLY[language], language, ctx.handoff,
                              tools_used, "llm_error", [str(exc)])

        text = response.text.strip()
        if response.tool_calls or response.stop_reason == "refusal" or not text:
            reason = "too many tool rounds" if response.tool_calls else response.stop_reason
            return self._fallback(ctx, language, tools_used, [f"no usable reply ({reason})"], response.provider)

        problems = check_reply(text, facts)
        if not problems:
            return AgentReply(text, language, ctx.handoff, tools_used, "ok", provider=response.provider)

        log.warning("Guardrail rewrite for %s: %s", ctx.phone, problems)
        rewritten = self._rewrite(messages, response, problems)
        if rewritten and not check_reply(rewritten.text, facts):
            return AgentReply(rewritten.text.strip(), language, ctx.handoff, tools_used, "rewritten",
                              [str(p) for p in problems], rewritten.provider)
        return self._fallback(ctx, language, tools_used, [str(p) for p in problems], response.provider)

    def _rewrite(self, messages: list[ChatMessage], draft: LLMResponse,
                 problems: list[Violation]) -> LLMResponse | None:
        instruction = prompts.REWRITE_INSTRUCTION.format(problems="; ".join(str(p) for p in problems))
        try:
            # No tools on the rewrite: it must only rephrase what it already knows.
            response = self.llm.complete(self.system_prompt,
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
