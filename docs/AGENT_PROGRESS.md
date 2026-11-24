# Agent progress (update at every commit)

Spec: docs/AI_AGENT_DESIGN.md. Commit format: `Step AI-n: ...`. Tests: `.venv\Scripts\python.exe -m pytest tests/resort -o addopts=""`.

## Done
- AI-0 rules + design doc: b7befa4 (price guard follow-up 2ee3542)
- AI-1 skeleton, Enquiry state, dry-run outbox: 0598406
- AI-2a update_enquiry + get_price: 05623a3
- AI-2b photo flow + send_photos: 820c015
- AI-2c remaining tools + TOOL_SCHEMAS (11 tools), staff alerts through the outbox, dry run never calls WhatsApp: 916b5d7
- early_checkin flag on update_enquiry: 7b07d82
- AI-3 prompt + loop: e6c2042 (`agent/prompt.py`, `agent/turn.py`, optional `temperature` on the LLM providers)
- Owner rule 2026-11-24 (old bot AND agent): 45bf370 (3 adults per room at most, extra bed asked once and never automatic,
  adults above 6 years only, new form text, see below)
- Owner rule follow-up: a layout change after the link closes the old link AT ONCE (before the extra-bed answer); see below

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
- OWNER RULE 2026-11-24 (rules/resort_booking/RULES.md section 5): persons = ADULTS above 6 years only, children are never asked
  or counted. 2 adults per room by default; if the guest chooses fewer rooms, up to 3 adults in one room, never more
  (`availability.room_cap`, `base_per_room`, `rooms_with_three`). An extra bed is NEVER added by itself: only when a room will
  hold 3 adults (and the resort has an extra-bed rate) the fixed text `ask_extra_bed` ("Do you need an extra bed?") is sent ONCE
  per layout; yes = charged per bed per night, no = nothing; a bed the guest asks for is charged (max one per room).
  Agent: `update_enquiry` returns next_step `ask_extra_bed`, the answer comes back as `extra_beds` (0 = no), `create_booking`
  waits. Old bot: the question replaces the name ask once; the AI answers through `prepare_booking(extra_beds=N)` (it does not
  block the booking). Form line: "Number of adults (above 6 years) :". Old-bot parsing still reads a reply to the old wording.
- LAYOUT CHANGE AFTER A LINK (agent): when the new stay needs the extra-bed question, `update_enquiry` closes the old link at once
  (`close_old_holds`: the old booking is expired and its Razorpay link cancelled), keeps the guest's name and mobile in
  `Enquiry.contact`, sends the question, and when the answer (`extra_beds`, 0 = no) arrives it makes the new summary and link
  straight away (no name ask). A change that needs no question still relinks at once; the same stay as the link changes nothing.
  The old bot already closed the old link at once (its `_relink` runs in the same turn as the question).
- EXTRA BEDS, HOW MANY (owner, 2026-11-25, `Extra beds: how-many question`): exactly 1 room holds 3 adults -> `ask_extra_bed`
  (yes/no, yes = 1). 2 or more -> the new fixed text `ask_extra_beds` ("How many extra beds do you need?", all 4 language
  variants in i18n.py; `Enquiry.checked["extra_bed_max"]` picks the text in `photos_plan.open_step` too). next_step stays
  `ask_extra_bed`; the answer is `update_enquiry(extra_beds=N)`, N from 0 up to the 3-adult rooms (`rooms_with_three`). More is
  capped there and the fixed text `extra_beds_max` says the maximum; facts carry `extra_beds_max`. Not tied to one-per-room any
  more in that case (the old "at most one per room" refusal stays only for a bed asked for where no room holds 3 adults).
  Old bot (chat.py) is not changed.
- Fixed on the way: the "same stay as the open link" check in `update_enquiry` compared the NEW number of adults with itself, so
  a change of persons alone (2 -> 3 adults, same rooms) looked like "unchanged"; it now uses the adults on the link.
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
