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

## Fix round 2 (2026-11-25): eval failures, no repeats, team wording, policies
- Root causes found in the round-1 transcripts:
  - change after a link sent no summary: `update_enquiry` built "the stay on the link" with the NEW dates, so a date-only
    change looked like the same stay ("unchanged"). It now uses the held booking's own dates. `room_picked` is not sent
    when the AI only re-passes the room already on the link.
  - guest_called text: the i18n text was right, but `bot._text` (`_ease_sir`, old-bot code) took "sir" out past the
    chat's start. The outbox now sends EXACT_KEYS (`guest_called`) word for word (`outbox._send_exact`: English letters,
    no booking id, logged; no sir easing). The AI's own line is dropped after it.
  - link_lost: the scenario waits 1 h, so the hold had run out and the AI explained the expiry. `send_payment_link` on
    an expired hold now re-checks the same stay (`_renew`: name + mobile from the old booking -> update_enquiry's relink):
    new summary + link, or the owner's no-room text.
  - day_without_month: the AI left month_given out. It is REQUIRED in the description; a past check-in without it is
    sent back to the AI (ok=false, nothing to the guest). A future check-in needs nothing (the value changes nothing).
- Descriptions / prompt: a place name alone IS booking information; language enum with example words per style; a wanted
  room type -> room_picks, exclude_room_types only for a refused type; link not working / again / lost -> reason lost;
  policies (pets, alcohol, couples, smoking, visitors, food ...) from FACTS in one line, else talk_to_owner(cannot_answer).
- Guard (AI's own line only): "How may I assist you" only on the first greeting (mid-chat: sentence dropped, nothing left
  -> "Hi sir."); "sent / shared / resent / passed your question / informed the owner" dropped when no sending tool ran;
  hold / expiry talk dropped (expir*, valid for, a minutes count next to link / hold / pay words); "owner" -> "team".
- No repeats: `Outbox.dedupe()` before every flush: the AI's line goes when it is (nearly, 90%) the same as a tool text;
  of two (nearly) equal tool texts only the first stays. ONCE_TOOLS (send_payment_link, send_photos, create_booking,
  check_payment, talk_to_owner, send_map) run once per turn; a second call returns `already_done`, sends nothing.
- Team wording: `handoff` = "Thank you. Please contact the team directly: {numbers}." in every language variant;
  ai_down / owner_will_help / pay_failed_link say team too. talk_to_owner: cannot_answer / call_request / other ->
  only the handoff text (no AI line, no open step / link reminder); guest_called -> exact thank-you + open step, no AI
  line. Staff alerts only for pay_terms, change_or_cancel_paid and other (no alert for cannot_answer, call_request,
  guest_called).
- Judge: sees `BOT:` (the AI's own line, scored) and `SYSTEM:` lines (code's texts, 140 chars, never scored or blamed,
  but they count as answering the guest).
- Scenarios: ten_adults expects `ask_extra_beds`; link_lost expects summary + pay; new link_not_working,
  policy_in_facts, policy_not_in_facts; no staff alert on call_request / three_questions (41 scenarios).
- Old-bot tests: the 5 pay_terms tests and `test_ok_to_the_owner_numbers...` (flow.py spots its numbers message by
  "contact the owner") are skipped with a reason; wording-only expectations moved to "team". Old-bot code untouched.
- Tests: tests/resort/test_agent_fix_round2.py (67). Full tests/resort: 1111 passed, 6 skipped.
- Eval after round 2 (2026-11-25, sarvam_chat -> groq, 41 scenarios): 24/41 scenarios pass 3/3, 85/123 runs, 6.7 min
  (round 1: 18/38, 70/114). Now passing: change_dates_after_link, link_lost, link_not_working, ten_adults, call_request,
  three_questions, policy_in_facts, pay_terms x3, question_with_link_open. Still failing (causes from the transcripts):
  - past_date (0/3, NEW): "20 Nov 2026" -> the AI left month_given out, was sent back, then passed false -> moved to Dec.
  - day_without_month (0/3): "20 to 22" -> the AI passes month_given=true (wrong value now, not missing).
  - question_then_open_step (0/3, NEW): parking is not in FACTS -> talk_to_owner(cannot_answer) -> handoff only, no open
    step (the new Part-3 rule). The scenario's expectation conflicts with that rule: owner decision needed.
  - restart (0/3): after restart, "I need a room" gets the greeting again and no update_enquiry.
  - happy_tanglish / happy_manglish: language still wrong in some runs (None / en / hinglish); "room venam" with 2 open
    resorts -> the AI passed a place itself (form instead of ask_location).
  - happy_en 2/3: "Alleppey" alone still missed once. policy_not_in_facts 1/3: pets answered without talk_to_owner.
  - tone only: photos_* (the AI's line repeats the open step), discount, guest_called 1/3, mid_chat_hi ("Hi sir." is
    eased to "Hi." by the old send path; the judge calls it cold), thanks_with_link_open, unrelated_question.

## Fix round 3 (2026-11-25): round-2 eval fixes, short human replies, room labels and photos
- Dates: `month_given` is gone. update_enquiry takes check_in / check_out as {day, month, year} ("Copy only what the guest
  said. Never fill month or year yourself."); code builds them (`tools.build_check_in` / `build_check_out`): no month ->
  the next time that day comes; no year -> the next time that date comes; a full date that has passed -> past_date (the
  old "more than 14 days ago -> next year" shift is gone); check_out with no month -> the check-in's month, or the next
  month when it would not be after the check-in. nights still works. A full 'YYYY-MM-DD' string is still taken.
- Place: update_enquiry takes a place only if it (or a dashboard name / alias of the resort it stands for, as written or
  misspelt: `same_name`, plain string code, no regex) is in the guest's messages of this conversation; else it is ignored
  (facts `place_ignored`) and the location question goes. `AgentTools(guest_text=...)`: run_turn passes the guest's message;
  None = a call outside a guest turn (tests, `_renew`), not checked.
- Not our place: new fixed text `not_our_place` ("Sorry, we have properties only in {towns}.", en/mn/tn/hn; towns = the
  OPEN resorts from the dashboard, `tools.towns_and`), then the location question unless it is the last message (then an
  outbox marker `step_shown` keeps the turn's open step from adding it). One resort only: the text, then the booking goes
  on with that resort. The prompt's Kochi example gets the same dashboard towns.
- Restart: root cause = the gate logged the greeting BEFORE the `[restart]` mark, so the next message ("I need a room")
  looked like the first message of a new conversation (no greeting in view, `fresh=True`) and the AI greeted again. The
  mark now goes first; a greeting before the guest's first message reaches the AI as `[You already said: "..."]`;
  `fresh` = nothing sent in this conversation yet (`turn.greeted`).
- Language: `tools._style`: any case; missing / invalid -> the previous turn's language.
- talk_to_owner(cannot_answer): the handoff text, THEN the code's open step (form / name+mobile / link) unless it is the
  last message (cannot_answer left NO_STEP_REASONS; call_request / other still get only the handoff).
- Prompt: "If the answer is not written in FACTS, you do NOT know it. Call talk_to_owner(cannot_answer). Never answer
  policy questions ... from general knowledge."; new HOW YOU TALK block (busy, warm front-desk person; answer first;
  examples); "sir" once at the start of a chat and in short greetings.
- Sir: `bot_outbox` sends EVERY agent text through `_send_exact` (English letters, no booking id, logged) - the old send
  path's `_ease_sir` never touches agent messages.
- Guard (AI's own line): more than 2 sentences / 30 words -> ONE retry ("Too long. Say the same in under 20 words, one
  sentence if possible."), still too long -> the first sentence. Banned phrases (anything else, is there anything, other
  listings / hotels / options, feel free, don't hesitate, I'm sorry I only, I don't have details, as an AI, please note
  that, I can help you with, system, automatically, the tool, owner) -> the same retry, then the sentence is dropped.
- Judge: gets the owner's style rules (short, answer-first, a short reply is not cold; code sends the owner's texts, open
  steps, links and summaries).
- Rooms: the guest's OWN unpaid hold never counts as taken for the room number they would get (`pp.next_numbers(phone=)`;
  the room checks already used `free_rooms(own)`). After a room check: rooms_ok -> rooms sentence -> glimpse -> general
  photos not sent yet -> FOR EACH offered room "*Room <number> - <room type>*" + that room's photos (a label even with no
  photo; `pp.offered_sets`) -> ask_contact. A label the last room check already sent (`checked["labels"]`, or the chat
  log) goes again only with new photos (the extra-bed answer re-checks the same rooms: no second label). Found on the way: `Outbox.dedupe` dropped the second label ("Room 101" vs
  "Room 102" are 90% alike); `near_same` now never matches texts whose numbers differ.
- Room choice: `room_picked` only when the GUEST chose a room (room_picks with a type or number), never for the code's
  pick by priority or an exclude-only change. send_photos(other_room / room_type) for ANOTHER room than the offered one:
  "Yes, X is also free for your dates..." (`other_room_free`) + label + photos (`Plan.also_free`), photos already sent ->
  `other_free`; not free -> room_not_free / no_other_room. It only shows, never switches.
- Test data: `scripts/agent_test_data.py` (eval scratch DB and `chat_resort.py --agent`): every resort on, general photos,
  room numbers (101.. / 201..) and one own photo per room number. chat_resort prints each photo as a `[photo] <name>` line
  under its label. The eval checks `check_in` / `check_out` against the date CODE built (the tool result's facts).
- New evals: not_our_place, place_never_guessed, check_out_next_month, room_labels_each_room,
  other_room_shown_not_switched, short_human_reply; day_without_month checks the built dates; restart turn 3 expects
  update_enquiry and no "How may I assist" (47 scenarios). Tests: tests/resort/test_agent_fix_round3.py.
- Part 2 c) "Guest CHOOSES ..." was cut off in the round-3 message: done in round 4.

## Fix round 4 (2026-11-25): room choice, photos anytime, recent past dates, eval scenarios
- Room choice: prompt + room_picks description: the guest CHOOSES a room (this one, take it, change to X, 'cottage venam',
  'room 103', a swipe-reply 'this one venam' on a room's photo or label) -> update_enquiry with room_picks; only asking
  about / seeing another room -> send_photos. The code already did the rest: room_picked + the new rooms sentence; with a
  link open the old link is closed, then room_picked, rooms sentence, new summary + new link (`_relink`). A room number
  pick maps to its room type (as in the old bot; the exact number is allocated by priority at payment).
- Photos at any stage: prompt + send_photos description: "Guest asks to see rooms or the resort in any words -> call
  send_photos. Never refuse, never ask for dates first." After payment (Enquiry.paid or a paid booking for the stay,
  `_paid_stay`) photos are planned without dates, so the guest's own paid booking never makes their room "not free".
  default / more after the offered room was shown: the next free room type, then the next free room NUMBER of the same
  types with its own photos (`pp.more_numbers`, labelled "*Room 109 - ...*"), then the resort's general photos, then
  photos_sent. `queue_room` keeps a room number's own label.
- Dates (owner rule): a date WITHOUT a year that passed up to 30 days ago (`RECENT_PAST_DAYS`) -> past_date ("20 Nov has
  already passed. ..."); more than 30 days ago -> next year (25 Nov: "10 Jan" -> 10 Jan 2027). Works over the new year
  (5 Jan: "28 Dec" -> past_date). A day alone still means the next time it comes.
- Eval checks (scripts/eval_agent.py, documented at the top of the scenarios file): max_words (the AI's own line),
  ai_empty, ai_required, in_order ("a|b" = either), only_sent, no_duplicates; scenario `closed: [name]` switches a resort
  off. 15 new scenarios (62 in all); not_our_place got its must_not. Differences from the request: scenarios with no
  place in the message got "Alleppey" (two open resorts in the eval, so the location question would come first);
  not_our_place_2 uses Ooty, because Munnar is Kanthalloor's own town in the dashboard ("Kanthalloor, Munnar") - a new
  munnar_is_kanthalloor scenario checks it counts as ours.
- Tests: tests/resort/test_agent_fix_round4.py.

## Fix round 5 (2026-11-25): from the round-4 eval root causes (36/62 scenarios, 133/186 runs)
- Booking talk to tools: HOW YOU TALK keeps only non-booking examples (pool, parking, is it secure, weather; the Kochi
  example was copied word for word by the AI). New prompt lines: place / resort / dates / persons / rooms = booking
  information -> update_enquiry; "ok" / "thanks" with nothing to answer -> write nothing. Guard (`guard.booking_talk`, AI
  text only): the line names a dashboard resort / town (`place_names`, aliases included) together with rooms / property /
  available / a question, or says "we have properties", or asks which place / resort, and neither update_enquiry nor a
  sending tool ran -> ONE retry ("This is booking information. Call update_enquiry instead of answering yourself."); still
  no call -> the line is dropped and the code's open step / quiet reply goes. "December in Alleppey is cool" is not caught.
- Shorter lines: guard limit 1 sentence / 20 words (retry "Say it in one short sentence, under 15 words.", then the first
  sentence). When tools sent messages this turn, the AI's line is dropped if it only talks about them
  (`guard.restates_tools`: photos / map / form / link / here are / below / sent ..., or half its words are in the tools'
  texts). Prompt: "Answer only what was asked. No extra selling points unless the guest asks about the resort."
- No re-sent steps: `due_step` skips a step among the last 2 messages we sent (`pp.recent_texts`; for pay: any text with
  the same link) when only the AI spoke this turn; when a fixed text spoke (a handoff, photos) the step still follows
  (round-3 rule e). Nothing at all went out (`turn.quiet_reply`): open unpaid link -> `pay_nudge` once ("Please complete
  the advance payment to confirm your booking."), when pay_nudge or the link is among the last 2 -> "Sure sir." (`sure`);
  else the open step when it is not among the last 2; else "Sure sir.", nothing when that was the last message; a first
  message with nothing known still gets say_again. New i18n keys pay_nudge, sure (4 variants each).
- Language: `language` is REQUIRED in every tool schema that has it (all but my_bookings / recall_old_chat); code still
  falls back to the last language.
- Dates: check_in / check_out description: "'20 Nov' → {day: 20, month: 11}. A month word the guest wrote is ALWAYS copied.
  Only leave month null if the guest wrote no month."
- send_photos(all_rooms) with everything shown: the default / more fallback (next room number, then general photos, then
  photos_sent), never no_other_room.
- Owner rules in tool descriptions: talk_to_owner "NOT for the first discount ask ..."; send_payment_link "hold, reserve,
  pay later/tonight/tomorrow with an open enquiry -> reason hold". update_enquiry facts carry `resort` (the resort code
  resolved); the eval checks `place` against it (free_text_one_message: 'kanthaloor' is right).
- Tests: tests/resort/test_agent_fix_round5.py; earlier tests moved to the new rules (no step re-sent right after it went,
  one-sentence replies, language required, no Kochi example).

## AI-6 setup (2026-11-25): tester allowlist for the test WhatsApp number
- `AGENT_TEST_PHONES` (config `agent_test_phones`, comma-separated, normalized to E.164): with AGENT_MODE=agent only these
  guest phones get the agent; any other phone -> no reply, the message is only de-duplicated, and a log line "Agent:
  <masked> is not a tester: no reply" (`gate.is_tester`, outcome `not_a_tester`). Empty = the agent answers everyone.
  AGENT_MODE stays global (one switch for the whole service); the allowlist narrows it per phone. AGENT_MODE=old ignores it.
- `STAFF_ALERT_PHONES` (config `staff_alert_phones`): when set, EVERY staff alert (agent and old code, payment webhook,
  sweeper) goes only to these numbers: `gate.staff_override` replaces `staff_numbers()` on the one bot object built in
  `services.ResortServices.bot`. Empty = as before (RESORT_STAFF_NUMBERS + each resort's dashboard staff numbers).
- Payments: RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (test keys start with rzp_test_; both .env and data/railway_resort.env
  already hold test-mode keys), RAZORPAY_WEBHOOK_SECRET for /resort/razorpay/webhook (empty today: only check_payment's
  polling confirms payments until it is set).
- tests/conftest.py blanks AGENT_TEST_PHONES and STAFF_ALERT_PHONES (a local .env never leaks into tests). Tests:
  tests/resort/test_agent_tester_switch.py.

## Live-test fixes (2026-11-25, owner test on the test number)
- Why "What sure" / "Send" got nothing (from the code; no Railway CLI here to read the logs): round 5's `quiet_reply`
  returned NOTHING when the last bot message was already "Sure sir." and the open step was among the last 2 messages,
  and the AI had written nothing (or a guard dropped its line). The turn then ended with an empty outbox. Log lines
  to confirm on Railway: `Resort message wamid...: never_silent` with no send after it, `Guard: ... dropped`.
- NEVER SILENT: when a turn would send nothing, (`turn.quiet_reply` first: pay_nudge / a step not sent lately / "Sure."
  only when NO step is open) -> a) the AI is asked once more (internal `SECOND_CHANCE` message, never shown; its tools run,
  its line passes every guard) -> b) still nothing: the open step again (`open_step_now`, a repeat is allowed; the live
  link's pay text first) or "Sorry, could you say that again?" (`say_again_short`). An open step = a booking under way
  (`Enquiry.active`) or this turn's own booking ask / answer was dropped by the guard.
- Unclear asks: new tool `repeat_step` (12 tools now; ONCE): the open step again (link / form / name ask / location /
  extra bed), repeat allowed; the AI decides ("ok send me", "send", "what", "what sure"); NOT for a plain ok / thanks.
  Prompt: "... → repeat_step. Never just "Sure"."
- Photo order: `Outbox.flush` sends one item after the other (each send returns first; photos go one by one in
  `bot._send_photos`) and a text right after photos waits `AGENT_PHOTO_GAP_S` (default 1 s; tests 0). The open-step
  texts (`STEP_TEXT_KEYS`: ask_location, form, extra bed, ask_contact, pay, hold_pay, pay_nudge) are moved to the END of
  every turn (`Outbox.move_last`), so ask_contact is always the last message of the room-check flow.
- "sir" only in the first reply: after the first reply of a conversation (`fresh`) the guard removes sir / madam from the
  AI's line and fixes the sentence (`guard.drop_sir`); MID_CHAT_HI = "Hi."; fallbacks "Sure." / "Sorry, could you say that
  again?" (all 4 languages); the prompt has one rule ("Say 'sir' (or 'madam') only in your very first reply of the chat.
  Never again after that."); the owner's fixed texts in i18n.py are untouched (guest_called keeps its "sir").
- No offers: banned phrases + "let me know if", "if you want to see", "I can also", "would you like".
- Tests: tests/resort/test_agent_live_fixes.py; earlier tests moved to the new rules (Sure. / Hi. / say_again_short,
  the open step last, 12 tools, never silent instead of nothing).

## Next
- AI-6: the owner tests on the test number (Railway: AGENT_MODE=agent + AGENT_TEST_PHONES + STAFF_ALERT_PHONES).
- Run the real-AI eval (the user runs `scripts\eval_agent.py`), then fix what fails with general rules.
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

## Claude Haiku + prompt caching + lean requests (2026-11-25)
- Provider: the existing `AnthropicLLM` (app/providers/llm/anthropic.py) is the resort chain's `anthropic` entry
  (clients/resort/resort.yaml): key `ANTHROPIC_API_KEY`, model `AGENT_MODEL` (never hard-coded). Default chain
  `anthropic,sarvam_chat,groq`; an Anthropic error moves to Sarvam, then Groq (FallbackLLM). A missing key or model:
  skipped with the log line "LLM provider skipped: anthropic: ...". Sarvam speech-to-text is unchanged.
- Caching: request = tools [cache breakpoint] -> static system (rules, style, OWNER RULES, FACTS) [cache breakpoint] ->
  dynamic system (TODAY, GUEST RECORDS, CURRENT ENQUIRY) -> chat. prompt.py puts `CACHE_SPLIT` between the two parts;
  OpenAI-style providers get one plain text (`plain_system`). The swipe note stays on the guest's message.
- Lean: HISTORY 12, MAX_ROUNDS 4, max_tokens 300, temperature 0.2, no extended thinking. Tool results go back as compact
  JSON without empty fields; when a new tool round starts, the earlier round's results shrink to one line
  (`[earlier result] update_enquiry: ok, ..., sent ask_contact, next ask_contact`). Earlier turns never carried tool JSON
  (the history is the chat log). Tool descriptions shortened (12.8k -> 9.3k chars) without repeating the prompt.
- Measure: `app/resort/agent/usage.py` logs "Agent tokens: model=.. calls=.. input=.. cache_read=.. cache_write=..
  output=.. rounds=.." per turn and appends to agent_tokens.jsonl next to the database (no guest text, no phone: a
  12-char hash). `scripts/token_report.py` prints averages per turn / per booking chat and the cache hit rate. The eval
  prints tokens per scenario and in SUMMARY.
- Tests: tests/resort/test_agent_haiku_tokens.py (fake clients only).
- Fix (same day): anthropic SDK 1.15 sends API version 2026-10-21, which removed `temperature` / `top_p` / `top_k`
  (Claude Haiku 5.5 accepts only the default sampling anyway). The Claude request no longer sends temperature (the
  other providers still get 0.2); resort.yaml sends `thinking: {type: disabled}` for Claude. FallbackLLM now moves on
  after ANY exception from a provider (one log line "LLM provider X crashed, trying next"), and AnthropicLLM wraps any
  SDK exception in LLMError. Live: 3 scenarios x 3 runs on Haiku, cache hit 92%, ~17k tokens per AI turn (2-3 calls).
  photos_after_link fails: the room photos were already sent in turn 1, so 'send room pics' gets photos_sent (code rule).
