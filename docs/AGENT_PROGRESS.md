# Agent progress (update at every commit)

Spec: docs/AI_AGENT_DESIGN.md. Commit format: `Step AI-n: ...`. Tests: `.venv\Scripts\python.exe -m pytest tests/resort -o addopts=""`.

## Done
- AI-0 rules + design doc: b7befa4 (price guard follow-up 2ee3542)
- AI-1 skeleton, Enquiry state, dry-run outbox: 0598406
- AI-2a update_enquiry + get_price: 05623a3
- AI-2b photo flow + send_photos: 820c015
- AI-2c remaining tools + TOOL_SCHEMAS (11 tools), staff alerts through the outbox, dry run never calls WhatsApp: 916b5d7
- early_checkin flag on update_enquiry: 7b07d82
- AI-3 prompt + loop (this commit): `agent/prompt.py`, `agent/turn.py`, optional `temperature` on the LLM providers

## Next
- AI-4 gate (dedupe, voice, STOP/START, restart, pause, lock) + guard (design section 6, retry with feedback) + outbox rules
  (never the same text twice). `run_turn` has NO guardrails on the AI's own words yet.
- Then AI-5 evals, AI-6 shadow, AI-7 switch, AI-8 cleanup.

## How a turn works now (agent/turn.py)
- `run_turn(bot, llm, phone, text, outbox, swipe="")`: system prompt + last 20 real messages (conversation = since a restart or a
  24 h gap) + the guest message (swipe line just before it) -> LLM <-> tools (max 6 rounds, temperature 0.3, 700 tokens)
  -> outbox: the AI's own line first, then the tools' messages in queue order, then the open step if nothing went out.
- LLM failure: `ai_down` text (only if nothing was sent) + one staff alert per 30 min (shared with the old bot's `_down_alerted`;
  a dry run never uses up the slot).
- The swipe line is built by `swipe_note(bot, phone, context_id)`; the gate (AI-4) will call it.
- The model comes from LLM_CHAIN only (nothing in the agent names a model).

## Decisions and facts to remember
- `temperature` is an optional argument of `complete()` (None = the provider's own); only the agent passes 0.3.
- The swipe-reply note is on the guest's message, not in the system prompt (keeps the prompt cacheable).
- Tools never read guest text; `_Turn.quiet` makes the reused prepare/create code read nothing from the old chat log and send
  nothing past the outbox. The name for a booking comes only from the tool argument (empty allowed).
- No prices, rates or amounts in FACTS or the prompt; amounts only from tool results (`facts.price`).
- `create_booking` takes name / mobile / language only; early check-in and extra beds come from the saved Enquiry.

## Open issues
- A dry run keeps WhatsApp and staff alerts off, but `create_booking` / `update_enquiry` still write to the database and make a
  payment link. AI-6 shadow mode needs a scratch database and mock payments.
- Photo / room-label helpers exist twice (chat.py and agent/photos_plan.py) until AI-8.
- `get_price` also still exists in chat.py (the old bot offers it until AI-8).
- Old chat.py `other_room_free` line is said whenever kind=other_room and rooms are checked (the old regex "not a photo word"
  condition cannot be kept).
