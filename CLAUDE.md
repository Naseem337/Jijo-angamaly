# CLAUDE.md: handoff notes for Claude Code

Multi-client WhatsApp AI agent for pre-owned car dealers. First client: **True Choice** (Kerala),
in Malayalam, Manglish and English. **PLAN.md is the spec.** PLAN.md §9 "Decisions" overrides
everything above it. Build order is PLAN.md §6.

## How the user wants to work

- Build **one PLAN.md step at a time**. Commit each step as `Step N: ...`. End every step with
  **PowerShell** commands to run and test it, plus what comes next. The user is on Windows and uses
  the VS Code terminal.
- **Setup the user does by hand** (Meta, API keys, AWS, `.env`): give **one small step per message**,
  with exact clicks and URLs, then wait for "done". Long multi-part guides confused them. Use simple
  language. Pre-fill files, write helper scripts, and check their work yourself instead of asking them
  to type commands.
- **Never hardcode a Claude model name.** It comes from `ANTHROPIC_MODEL` in `.env`.
- Never print or commit secrets. `.env` is gitignored. When checking it, report only whether a value
  is filled in and its length.
- Run tests with `.venv\Scripts\python.exe -m pytest -q`. Code must stay 3.12-compatible, because
  Lambda runs 3.12 and the local machine has 3.13.

## Status (last updated 2026-10-16)

| Step | State |
|---|---|
| 1 Scaffold, config, phone/money/language/clock | Done |
| 2 Storage: models, Repository, SQLite | Done |
| 3 Inventory: 30-car mock JSON, mock adapter, real stub, model aliases | Done (`python -m app.inventory --client truechoice --model swift --budget 7L`) |
| 4 LLM providers: OpenAI-compatible, Anthropic SDK, mock, fallback chain | Done (`python -m app.providers.llm --client truechoice --each` checks real keys) |
| 5 Agent: prompt, tools, EMI, guardrails, voice confirmation, tool loop | Done (`python -m app.cli_chat --client truechoice`); tested live on Gemini |
| 6 WhatsApp: client, webhook, signature check, dedupe, router (text only) | **Done and live-tested on 2026-10-18**: the user chatted with the bot from their phone. Run it with `.venv\Scripts\python.exe scripts\dev_whatsapp.py`, which starts uvicorn plus a cloudflared quick tunnel and re-points Meta's webhook through the Graph API (app id from debug_token, app token = id\|secret). |
| 7 Voice in: download, length check, Sarvam STT, unclear check, read-back | Done; live-tested with Sarvam |
| 8 Voice out: spoken_text (LLM, digits checked by guardrails, then words), Sarvam bulbul:v3, ffmpeg OGG/Opus, upload, `voice: true`, daily char cap, media-id cache | Done; live chain tested |
| 9 Handoff (pause + staff summary), staff commands RESUME / LEAD / HELP, STOP/START with one confirmation, 24 h auto-resume, yard-visitor leads with YES consent | Done (`python -m app.leads --client truechoice` lists leads) |
| 10 Alert engine: match new cars to active leads + chat requirements, alert templates, dry-run CLI | Done (`python -m app.alerts --client truechoice --dry-run`); live send waits for Meta's template approval |
| **11 DynamoDB repository** | **NEXT** |
| 12–14 | Not started |

Steps 1–10 are done; the bot answers on WhatsApp through the test number.

Step 10 notes (app/alerts/):
- `AlertEngine.run(dry_run, pretend_new)`: pure code, zero AI tokens. Wishes = leads with status active/notified
  + chat requirements (customer.consent needed). A car counts if first seen after the wish was made and within
  `new_car_days` (7), so a run outside 09:00–20:00 IST or over a cap just defers. The first run seeds all stock
  with first-seen 2000-01-01 (never alerted).
- One template per customer per run: tc_car_alert_1 (photo header via `PhotoSender.media_id`) / _2 / _3; ranked
  exact first, then price closest to budget, then newest. At most one near match per 7 days. 1 message per
  customer per day (`per_customer_daily`). 3 alerts with no reply → paused until they write (derived from the
  alert log + `last_inbound_at`; there's no "still looking?" template any more).
- Alert rows are claimed `pending` before sending (never sent twice); failed / dry_run rows are retried.
  The sent alert is stored as an outbound message (source `template`), so the agent sees the offered cars
  when the customer replies. A lead becomes `notified` after its first alert and keeps getting alerts.
- Live test: give your own phone an active lead (LEAD + YES), then
  `python -m app.alerts --client truechoice --pretend-new TC-xxxxxx` sends that stock car as if it just arrived.
- Not scheduled yet: on AWS it's a daily EventBridge job (Step 13). Locally, run the CLI by hand.

Step 9 notes:
- Router order: dedupe → staff command → keywords (STOP / START / YES to a lead) → paused (auto-resume
  24 h) → voice → keywords again on the transcript → agent → handoff.
- `HandoffService`: tool handoffs pause the chat; system ones (`llm_unavailable`, `guardrail`) only
  notify. Staff numbers are never paused, because they double as test customers. The staff summary goes
  as text if the staff member messaged in the last 24 h, otherwise as the `staff_lead_alert` template
  (fails until Meta approves it; the failure is logged).
- `LeadService`: LEAD parsing = LLM JSON + rule-based gap filling. It still works when the AI is down
  (free-tier quota). The consent request is sent only inside the customer's 24 h window; otherwise staff
  are told "template needed".
- Step 10 hooks: read `repo.list_leads(status="active")` + `lead.requirement`; set status `notified`;
  respect `customer.opted_out`.

Agent design notes (app/agent/):
- `Agent.handle()` owns a whole customer turn: it stores the inbound and outbound messages, and the
  router in Step 6 should call it. The router itself handles opt-out, staff commands and paused chats.
- The system prompt depends only on config, so it's cache-friendly. Per-turn facts (reply language,
  time, pending read-back notes) go in `turn_context()`, put in front of the latest customer message.
- Guardrails: every ₹, km and year figure must come from a tool result, an earlier message, or the
  customer. Promise words are blocked. A failing reply gets one rewrite (with no tools); after that the
  bot sends a safe fallback and hands off.
- `AgentReply.handoff` is only a request. Step 9 does the actual pause, staff summary and customer notice.
- Gemini tool calls carry `extra_content.google.thought_signature`, which must be echoed back
  (`ToolCall.extra_content`).
- The Gemini free tier allows **5 requests/min per model**, and one customer turn uses 2–3 requests,
  so the backup model often answers.
Tests: all pass (`.venv\Scripts\python.exe -m pytest -q`). tests/conftest.py blanks LLM_CHAIN so a
local `.env` override can't change test results.

## External setup done by the user

- **Meta / WhatsApp test number** works: +1 555 151 2035, Phone number ID `1414805245056988`.
  - A template and a free-form text were both received on the user's phone.
  - The permanent system-user token (`whatsapp-bot`, never expires) is in `.env` and was verified
    against Graph API **v25.0**. The config was moved from v21.0 to v25.0.
  - App secret (32 hex characters) and staff number are in `.env`.
  - The verify token was generated into `.env`.
  - The webhook is configured (Meta app id 1116318957515906, `messages` subscribed). Quick-tunnel URLs
    change on every start, and `scripts/dev_whatsapp.py` updates Meta automatically. Quick tunnels die
    when the laptop sleeps.
  - cloudflared is a plain exe at `%USERPROFILE%\tools\cloudflared.exe` (winget needed admin, which
    the user missed).
  - If port 8000 is busy, check for an old `uvicorn --reload` and its `spawn_main` child process.
- `scripts/send_test_message.ps1` sends a test text from the test number.
- **AI provider now: Groq free tier (2026-10-21).** Google blocked the Gemini account ("project denied access",
  and a new key in a new project was blocked too), so Gemini chat AND the Algenib voice are gone for now.
  - `.env`: `GROQ_API_KEY`, `LLM_CHAIN=groq,groq_backup`, `GROQ_MODEL=openai/gpt-oss-120b`,
    `GROQ_BACKUP_MODEL=openai/gpt-oss-20b`. Both write good Malayalam; `qwen/qwen3.8-27b` wrote gibberish Malayalam.
  - Groq free = **8,000 tokens/min per model** (~2 calls/min each; one customer turn is 2–3 calls of ~3.3k).
    On a 429 that says "try again in Xs" (≤15 s), FallbackLLM waits once and retries instead of apologising.
  - Voice replies are off until a TTS is chosen (Sarvam anand is ready via `tts_sarvam_option`).
  - The user plans a paid key later (Groq pay-as-you-go = same models, or DeepSeek).
- **Before that: Gemini free tier** (user's choice on 2026-10-17, no DeepSeek credit yet).
  - **Latency fix (2026-10-20):** gemini-3.8-flash kept timing out at 30 s on the free tier (over a minute
    per reply), so `.env` now has `GEMINI_MODEL=gemini-3.5-flash` (~3.5 s) and `GEMINI_BACKUP_MODEL=gemini-3.8-flash`.
    Gemini providers have `timeout_s: 12`, and FallbackLLM rests a provider for 120 s after a retryable failure.
  - `.env` has `GEMINI_API_KEY` and the two model names above
    and `LLM_CHAIN=gemini,gemini_backup`. Both models were verified with
    `python -m app.providers.llm --client truechoice --each`.
  - Gemini 3.x counts hidden thinking against `max_tokens`, so client.yaml gives it
    `thinking_headroom_tokens: 2048` and `reasoning_effort: low`. `gemini-2.5-*` is no longer offered
    to new accounts. The free tier sometimes returns 503 "high demand"; that's why there's a backup model.
  - The free tier may use chats for training, so it's for testing only. Go-live means removing
    `LLM_CHAIN` from `.env`, which restores the default deepseek → openrouter → anthropic chain with paid keys.
- **Sarvam** key is in `.env` (`SARVAM_API_KEY`). Facts checked live on 2026-10-18:
  - STT: `saaras:v4` with `language_code=unknown`, which returns `language_code` and `language_probability`.
  - A Meta media download timed out once at 15 s, so downloads now get 60 s plus one retry.
  - **Hard 30 s limit per request**, so longer notes are cut into 28 s WAV pieces with ffmpeg.
  - OGG/Opus is accepted, but only with MIME `audio/ogg` (`; codecs=opus` gets a 400).
  - `keyterms` (≤50) is set in client.yaml.
  - **Voice = Gemini TTS `gemini-3.8-flash-tts`, voice `Algenib` (user-approved, 2026-10-20), with Sarvam `anand`
    as backup** (VoiceOut tries them in order).
    - Style goes in a **director's-notes prompt** (`# AUDIO PROFILE … ### DIRECTOR'S NOTES … #### TRANSCRIPT`).
    - Tried and rejected: `"<style> Say: <text>"` read the instruction aloud, and `systemInstruction`
      returns 400 "Developer instruction is not enabled for this model".
    - Verify any change by transcribing the audio with Sarvam STT and checking no instruction words appear.
  - **Voice rules v2 (user, 2026-10-20):** text in → text out. Voice in → voice out with **no repeated words
    as text**. When there's car data, only the lines with a ₹ price or a car id follow as text
    (`car_details_text` in router.py), sent after the voice. If the voice fails, the full text is sent.
  - **Car names stay English in voice and text (user, 2026-10-20):** to_speech keeps numbers glued to letters
    or right after an English word (i20, XUV700, 4X4, Alto 800). Verified by verbatim STT (saaras:v3
    mode=verbatim): the voice says "ഐ ട്വന്റി" and "ആൾട്ടോ എയ്റ്റ് ഹൺഡ്രഡ്". Both prompts say car names
    are never translated or transliterated.
  - **One voice only, so the tone never changes:** Gemini Algenib with a "pleasant, gentle, medium pitch,
    never harsh or squeaky" style (transcript-verified). There's no tts_backup; Sarvam is documented as
    `tts_sarvam_option` (temperature 0.4, steady). Samples are in data/voice_samples/pleasant.
  - **Voice reply rules (user, 2026-10-20, superseded by v2 above):** a voice note in gets a voice note out with **no duplicate text**.
    When the reply has car data (search_cars / get_car_details / emi_estimate were used), the text is sent too
    and the voice summarises. If the voice fails, the text is sent instead.
  - **Address (user, 2026-10-21, replaces the 2026-10-20 sir/madam rule):** NO address words at all. Never
    the name, never sir/madam (no gender guessing), never chetta/chechi. The profile name is no longer sent to
    the AI. Code also enforces it: `without_names` + `without_titles` in agent.py clean every reply and voice script.
  - **24/7 (user, 2026-10-21):** the bot never brings up showroom hours or "closed now" unless the customer asks
    about timings or is picking a visit time (turn_context marks hours as background only).
  - **Voice rules (user, 2026-10-19, to cut cost):** voice only when the customer sent a voice note AND
    the bot has a real answer. Text only for: AI outage apologies, guardrail fallbacks, and
    "couldn't hear" / "too long".
  - **Gemini free tier = 20 requests/day per model** (quota id GenerateRequestsPerDayPerProjectPerModel-FreeTier).
    One customer turn uses 3–4 requests, so it ran out after a few test messages and every reply became
    the "technical problem" apology. Testing needs a paid model (DeepSeek top-up or Gemini billing).
  - Pricing (checked 2026-10-19): Gemini TTS costs $0.00225 per 10 s of audio until 2026-12-31, then double.
    Sarvam TTS costs ₹30 per 10k chars. Roughly ₹0.25 vs ₹0.60 per voice note.
  - Sarvam TTS: **only `bulbul:v3` speaks Malayalam** (v4-flash doesn't). Voice is **`anand` (male), to match the male persona (now Anees)**; the user noticed the female `kavya` voice didn't fit. Override it with `SARVAM_TTS_SPEAKER`.
    The user heard samples in data/voice_samples (kavya, priya, shreya, kavitha, anand, gokul, plus a
    Gemini TTS sample) but hasn't picked one yet. Malayalam needs native script, so Manglish replies are
    spoken in Malayalam script. A ~11 s note takes ~5 s to generate and is ~40 KB.
    Its fields are `text` and `language_code`, and it returns `audios` (base64 WAV). `opus` output is
    rejected on v3/v4-flash, so WAV must be converted to OGG/Opus with ffmpeg. Max 2500 chars.
- ffmpeg 9.0.2 is a plain exe at `%USERPROFILE%\tools\ffmpeg.exe` (found by `app.providers.audio.find_ffmpeg`,
  or `FFMPEG_PATH`). On Lambda it comes from a layer at /opt/bin.
- **WhatsApp templates submitted on the TEST account on 2026-10-21** (WABA id in `.env`
  `TRUECHOICE_WHATSAPP_WABA_ID`; the token can list and create templates via
  `GET/POST /{waba}/message_templates`): `tc_car_alert_1` (image header), `_2`, `_3`, `tc_staff_lead_alert`.
  - All are **Utility, English (`en`)**, with no buttons (user's low-cost plan; see docs/whatsapp_templates.md).
  - Dropped: the consent template (staff get the customer to send "Hi" at the yard, so the YES question is
    free) and `tc_still_looking`.
  - They must be submitted again in True Choice's own account at go-live.

## Decisions made in chat (not yet in PLAN.md)

- **WISH → LEAD, and what a lead is (2026-10-20, now also in PLAN.md §9):** staff commands are `RESUME` and `LEAD`.
  A lead is a yard visitor who didn't find their car and wants an alert. It needs the customer's YES, and
  STOP opts out all of their leads. The chat visit/test-drive record is now an **Enquiry** (`enquiries`
  table, `save_enquiry` tool). SQLiteRepository renames an old `leads` table (the one with an `interest`
  column) to `enquiries` on start.

- **Database:** SQLite locally, DynamoDB on AWS (Step 11). The user asked about Supabase. The reasons
  to keep DynamoDB: one AWS account, no server, near-zero cost, and Supabase free projects pause when
  idle. Switching would mean a Postgres repository. Undecided only if the user raises it again.
- **Inventory = True Choice's app backend, Carlet** (`api.carlet.in`, from 2026-10-20; `.env`
  `TRUECHOICE_INVENTORY_ADAPTER=carlet`). `app/inventory/carlet_adapter.py` reads `GET /api/car/list`
  (public, read-only, all cars at once, cached 5 min, last copy kept 1 h).
  - 174 cars in 4 showrooms: Makkaraparamba 113, Panangangara 24, Pattambi 23, Cochin 14. `car.location`
    is the showroom, and the prompt says which showroom. The `showrooms` option in client.yaml filters.
  - There's no single-car endpoint; ids are shown short (`TC-` + last 6 hex of `_id`), and the full id
    also works in get_car. `status: showroom` = available; unknown statuses are hidden.
  - `owners` is the ownership position (3 = "3rd owner", the user's clarification). There are new Car
    fields `max_loan` (not an approval), `insurance_valid_till`, `image_url` (WebP on S3: convert to JPEG
    before sending WhatsApp photos, which is a planned feature) and `source_id`.
  - **Car photos (user, 2026-10-20: only the first photo, to control cost):** the agent tool `send_car_photo`
    only picks car ids (max 3 per turn). `app/conversation/photos.py` sends them after the reply: download
    the WebP → ffmpeg JPEG ≤1280 px (~140 KB) → upload → `send_image` with a caption. The media id is
    cached 25 days. More photos or video → team handoff. Live-checked: 311 KB WebP → 136 KB JPEG →
    WhatsApp upload OK. The user confirmed: offer cars from all 4 showrooms.
  - The app also has POST /api/car/test-drive and /api/enquiry; pushing bookings there is an idea. Their
    format is unknown, so ask the app developer first.
  - Our own data (chats, leads, alerts) stays in our own DB, by user decision.
- **Before that, inventory was True Choice's Google Sheet** (shared on 2026-10-17; 300 cars; still supported). The sheet is
  "anyone with the link"; `app/inventory/sheet_adapter.py` reads it as CSV, refreshes every 5 minutes,
  and keeps the last good copy for up to 1 hour if Google is unreachable. Columns are matched by
  header name.
  - Columns since the user's update on 2026-10-17: Car ID, Make, Model, Body Type, Year, Fuel Type,
    Gearbox, Owners, Odometer, Expected Mileage, Tyre Condition, Color, Location, Price, Status.
    199 of the 300 cars are Available.
  - Status mapping: Available is offered. Booked, Sold and **Under Inspection (`coming_soon`)** are never
    offered. Unknown status wording is skipped with a warning, which is the safe default.
  - Body Type: "Compact SUV" maps to suv and "MUV/MPV" to muv.
  - **The sheet's `Location` column is the city the car is REGISTERED in** (user, 2026-10-19), not where it
    is; all cars are at the showroom. It's mapped by `inventory.options.columns: {registration: Location}`
    in client.yaml (per-client column overrides in sheet_adapter), and the prompt explains it.
  - The sheet id is only in `.env` (`TRUECHOICE_INVENTORY_ADAPTER=google_sheet`,
    `TRUECHOICE_INVENTORY_SHEET_ID`), never in git. Tests force the mock stock (tests/conftest.py).
  - So no inventory page is needed in the dashboard.
- **System prompt** = the user's own sales-persona prompt (shared on 2026-10-19), merged in
  `app/agent/prompts.py` with our tools, inventory-only and consent rules. Differences from the user's
  text: EMI gives the indicative tool range (only loan approval and rates are handed off), the bot never
  asks for the phone number, and the car id stays at the end of each car line.
  - Persona confirmed on 2026-10-19: `bot_name: Anees`, `city: Makkaraparamba, Malappuram, Kerala`.
    speaking_style says Malappuram showroom, with a light Malabar flavour.
  - `speaking_style` in business_rules.yaml (dialect and tone, plain words) feeds both the text prompt and
    the voice-script prompt.
- **Robustness (user, 2026-10-20: "handle any message a Malayali customer asks, not one-off fixes"):**
  - The prompt has HOW TO THINK (general rules), a COMMON SITUATIONS playbook (~30 intents) and DEALER FACTS
    (`dealer_facts` in business_rules.yaml; '(placeholder)' marks facts True Choice still has to confirm,
    and that mark is stripped from the prompt).
  - Exam: `scripts/live_eval.py` runs tests/eval/customer_scenarios.yaml (46 scenarios) against the real AI
    with automatic checks, and saves results to data/eval/. A full run needs ~150 AI calls, which means a
    paid key. Process: run it, fix the prompt with general rules (not per-message hacks), run again.
- **Token budget (user, 2026-10-20: "design for less token burning"):** measured on a live turn, a message
  went from ~6,900 to ~3,300 input tokens per AI call.
  - System prompt rewritten compact (16.6k → 8.8k chars, same rules). Tool specs trimmed (821 → ~600 tokens).
  - search_cars returns 3 cars as one `Car.to_line()` string each (430 → ~116 tokens; the guardrails still
    read every number).
  - History is 10 messages, with old ones cut to 300 chars.
  - LEAD notes of ≤8 words that the rules understand skip the AI (`RULES_ONLY_MAX_WORDS`).
  - Every turn's AI calls and tokens are in the outbound message meta `usage` and in daily counters
    `llm_*`; see them with `python -m app.usage --client truechoice`.
  - Step 10 matching must stay pure code (zero tokens). Keep new prompt text short; check the size with
    `len(build_system_prompt(cfg))`.
- Hosting is still the AWS plan. The user once pasted a Hetzner link, but that was a mistake.
- `purge_old_data` deletes old `pending_actions` (the interface docstring wins). The test was fixed
  to match.
- The anthropic SDK is 1.x (uses httpx2, so respx can't mock it). Its tests use a fake client object.
  OpenAI-compatible providers use httpx and are tested with respx.
