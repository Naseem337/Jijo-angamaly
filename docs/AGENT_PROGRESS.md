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

- AI-4 gate + guard + outbox: see the commit `Step AI-4: gate + guardrails + outbox` (`agent/gate.py`, `agent/guard.py`,
  tests `test_agent_gate.py`, `test_agent_guard.py`). The old bot is NOT live (CLAUDE.md): never edit flow.py / chat.py
  except the one routing line in `BookingBot.handle()`.

- Owner rules 2026-11-25: `guest_called` (4d11ea5) and `pay_terms` (8330e2b): talk_to_owner(reason=...) sends the owner's
  fixed text; guest_called alerts no one, pay_terms alerts the team. "Pay the link a bit later" = hold link; "pay later
  without the online advance" = pay_terms.
- AI-5 evals + chat tester: see the commit `Step AI-5: agent evals + chat tester`.

## Evals (AI-5)
- `tests/eval/agent_scenarios.yaml` (34 scenarios, format at the top of the file; N07 moved here as `guest_called`).
- `.venv\Scripts\python.exe scripts\eval_agent.py [--only id ...] [--runs N] [--no-judge]`: the REAL LLM_CHAIN, 3 runs each,
  a fresh scratch DB per run (seed + Kanthalloor switched ON + fake photos), fake payments, a recording fake WhatsApp,
  clock fixed at 2026-11-25 10:00 IST. `[paid]` marks the open link paid at the fake provider; `[image]`; `[wait Nh]`.
  Not a pure dry-run outbox: a dry run never logs the bot's messages, so later turns would not see them.
- Hard checks per turn: tools, args (last call), sent keys, not_sent, must_not, expect_text, gate outcome. Tone: one AI
  judge call per run, scores only the AI's own words 1-5, below 4 fails (a judge error is reported, never a fail).
- Failed runs: full transcripts in `tests/eval/out/` (git-ignored, wiped at the start of each run).
- `scripts\chat_resort.py --agent`: chat with the agent in the terminal (gate + agent + guard + outbox as live, printing
  fake WhatsApp, fake payment links, scratch DB with the seed).

- First full run (2026-11-25, chain sarvam_chat -> groq, no pause): 3/34 scenarios pass 3/3, 26/102 runs pass, 5.7 min.
  Main failures: no ask_location for "I need a room" (the AI asks in its own words); talk_to_owner reason is free text, never
  the exact `pay_terms` / `guest_called`; mid-chat "hi" gets no open step; changes after a link (dates) not sent to
  update_enquiry; Manglish "12 muthal 14" read as November; 10 adults in 4 rooms gives no how-many question; photos
  asked without a place: no send_photos; the AI's own line too long and repeating the fixed messages (judge).
  Judge quirk: it sometimes asks an English guest for Manglish style (the language hint in JUDGE lists all styles).
- `--pause N` adds N seconds between AI turns (default 0); a run where the AI was down is repeated once after 30 s.

## Fix round 1 (2026-11-25): open step + no repeats + owner reasons + dates
- OPEN STEP FROM CODE (turn.py `due_step`): at the end of every turn, while a booking is under way (`Enquiry.active`: a
  tool set `started`, or place / dates / adults / a link / a photo wish), if next_step needs the guest (ask_location,
  ask_details, ask_extra_bed, ask_contact, pay) and its owner text was not sent this turn (`STEP_KEYS`; pay = `pay` /
  `hold_pay`, details = `form` / `past_date`), the outbox appends it, unless it is already the last message we sent.
  Pay = the live link's `pay` text again. Never on a first message of a conversation with no tool call (a first "hi").
- guard f) `drop_detail_asks`: a SENTENCE of the AI's own that asks (?, please, share ... , parayamo, tharamo ...) AND names
  a booking detail (place / location / dates / check-in / adults / persons / rooms / name / mobile) is dropped; then the
  code's step is added even if no tool started the enquiry (`asked=True`).
- guard g) `repeats_tools`: when tools sent messages this turn, the AI's line is dropped (before the a/b/c checks, so no
  retry) if it has ₹ / a link / a date / summary|payment|advance|link / more than 25 words.
- Every tool note starts with "Already sent to the guest. Do not repeat it." when the tool sent the guest something
  (`AgentTools._result`). SENT_NOTE now: write NOTHING unless the guest asked something else, then one short sentence.
- talk_to_owner(reason) is an enum (`OWNER_REASONS`): pay_terms -> pay_terms text; guest_called -> thank-you, no alert, open
  step; cannot_answer / call_request / change_or_cancel_paid / other -> handoff text + staff alert; optional `staff_note`
  goes to the staff only. A free-text reason is taken as `other` with the text as the staff note.
- Dates: update_enquiry `month_given` (bool). false + the day already passed this month -> the same day next month
  (`_next_month_day`, a day the month lacks skips it), the check-out moves with it. TODAY block:
  "Today is Wed 25 Nov 2026 (2026-11-25) ... (12 → 12 Dec); pass month_given=false then." (`prompt.today_block`).
- One open resort only: update_enquiry and send_photos set the place themselves; ask_location is never sent. The eval DB
  and the test world have 2 open resorts (Pozhiyoram/Alleppey + Kanthalloor), so the question is right there;
  photos_resort now also expects update_enquiry(place=alleppey) + form on its first turn.
- Guard sources now include the saved room counts ("N rooms") of the enquiry.
- Judge: the language is judged against the guest message just before each line; turns with no own words are marked
  "[no own words]" and not scored.
- New evals: day_without_month, call_request, question_then_open_step, question_with_link_open; reason checks on
  discount (other) and three_questions (cannot_answer). Tests: tests/resort/test_agent_fix_round1.py (50).
- Diagnosis (not fixed yet): ten_adults_four_rooms_how_many: the AI's arguments and the code are right (adults 10,
  rooms 4, then extra_beds 2; code sends `ask_extra_beds` "How many extra beds do you need?"); the scenario expects the key
  `ask_extra_bed`, which is the yes/no text for ONE room, so the expectation is wrong. (Run 3: the AI greeted and called
  no tool on the first message: an AI miss.) change_room_type_after_link: run 1 = the AI's arguments (room_picks [] +
  exclude ["Standard Room"] instead of room_picks Family Suite, so the code correctly took the next room by priority,
  Deluxe); run 3 args right, it failed on tone (the AI repeated the summary), which guard g) now drops.
- Eval after round 1 (2026-11-25, sarvam_chat -> groq, 38 scenarios): 18/38 scenarios pass 3/3, 70/114 runs, 6.6 min
  (before: 3/34, 26/102). Still failing: "Alleppey" alone gets no update_enquiry (happy_en, happy_manglish,
  photos_resort); tanglish tagged manglish; day_without_month (the AI sends 20 Nov without month_given=false);
  guest_called text missing; mid-chat "hi" still gets "How may I assist you"; change_dates_after_link sends pay without
  a new summary; ten_adults (the scenario key, see the diagnosis below). Many tone fails blame the bot for the code's own open step / payment link
  ("included a fixed payment link"): the judge needs to see which lines the code added.
- Old-bot tests: 5 in test_never_silent.py (pay terms, expecting the pre-8330e2b text) fail since 8330e2b; the old
  bot is not edited.

## Next
- Fix the AI-5 eval failures with general rules (prompt / tool description / tool result), one at a time.
- AI-6 is now the OWNER TESTING on a test WhatsApp number with `AGENT_MODE=agent` (shadow mode dropped: no live traffic).
- Then AI-7 switch, AI-8 cleanup.

## Gate (agent/gate.py), AGENT_MODE=agent only
- `BookingBot.handle()` sends the message to `gate.handle(bot, msg)` when `config.agent_mode == "agent"`; `old` stays default.
- Order: dedupe (message id) -> media (reaction / sticker / edits / system = no reply; location / contacts / video /
  document = `unsupported`; voice -> the existing voice-in service, one retry, `too_long` / `unclear` not retried, failure =
  `voice_problem` + a chat row with the reason; image -> "[Guest sent an image]" (+ ": caption") for the AI) -> the message is
  saved -> per-guest lock -> exact `STOP` / `START` (state `opted_out` + guest `offers_consent`; START only after a STOP,
  STOP twice or START when not stopped goes to the AI; both work even while paused) -> staff pause (24 h auto-resume) ->
  exact `restart` (state cleared incl. the Enquiry, keeps `opted_out`, greet, RESTART_MARK) -> `run_turn` with `swipe_note`.
- Commands are the whole message only ("stop please" goes to the AI). `reset` is no longer a restart in the agent.
- Gate texts go through the outbox; `gate.handle(..., dry_run=True)` sends nothing (mark_read aside).

## Guard (agent/guard.py), only the AI's own line; tool messages are never checked, changed or dropped
- a) every ₹ amount (`₹`, `Rs`, `INR`, `/-`, `rupees`), date (12 Dec, Dec 12, 12/12, ISO) and room count ("N rooms") must be in
  this turn's tool results or tool texts, FACTS + the saved enquiry + today/tomorrow, or the guest's messages of this
  conversation. Room counts must match a room count there (not any number). NOTE: an amount from an EARLIER turn is not
  allowed (the AI must call get_price again), as the design says.
- b) booked / confirmed / reserved only if `Enquiry.paid` or a paid booking of this guest; a sentence with not / until /
  once / after / yet ... is allowed ("will be confirmed once you pay").
- c) promise words (will contact you, get back, one moment, please wait, let me check ...).
- a/b/c -> ONE retry: the bad reply + a "[CHECK] ..." user message saying exactly what was wrong; the retry may call tools
  (rounds count toward the same 6). Still bad -> `handoff` text (unless talk_to_owner already queued it) + `staff:guard`
  alert, outcome `handoff`. `TurnResult.guard` / `.retried` record it.
- d) stripped quietly: Razorpay / rzp (+ "via"), booking ids (flow.BOOKING_ID), lines in other scripts
  (chat.english_letters_only; all-native = no line, so the open step goes), links not from this turn's tools, the guest's
  name and first name (guest profile + Enquiry.contact).
- e) the AI's line identical to one already sent this conversation (bot._already_sent) is dropped, unless it carries a
  link from this turn. Dropped and nothing else queued -> never-silent open step.

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
  payment link (no longer blocking: shadow mode is dropped).
- Photo / room-label helpers exist twice (chat.py and agent/photos_plan.py) until AI-8.
- `get_price` also still exists in chat.py (the old bot offers it until AI-8).
- Old chat.py `other_room_free` line is said whenever kind=other_room and rooms are checked (the old regex "not a photo word"
  condition cannot be kept).
