# PLAN — Multi-client WhatsApp AI agent for pre-owned car dealerships

First client: **True Choice** (Kerala). Languages: Malayalam, English, Manglish (text and voice).
Build: 3 days, local-first; AWS (SAM) on day 3.

---

## 1. Architecture in one picture

```
WhatsApp Cloud API ──webhook──► FastAPI /webhook/{client_id}
                                   │  verify signature, dedupe message id, return 200 fast
                                   ▼
                              MessageRouter
          ┌──────────────┬─────────┴──────────┬──────────────────┐
     staff number?   opt-out word?        bot_paused?        normal customer
     StaffCommands   mark opted-out       store only         (voice → STT first)
     (RESUME, LEAD)  + confirm once       (staff sees it)          │
                                                                   ▼
                                                     Agent (LLM + tools + guardrails)
                                                     tools: search_cars, save_lead,
                                                     save_requirement, handoff_to_human,
                                                     emi_estimate
                                                                   │ {text, spoken_text}
                                                                   ▼
                                                     ReplySender (text always; voice per
                                                     policy "mirror" via TTS → OGG/Opus)

EventBridge cron(30 11 * * ? *) ─► AlertEngine (per client) ─► WhatsApp TEMPLATE messages
Staff phone browser ─► /dashboard (server-rendered Jinja2 + HTMX, per-client login)
```

Everything external sits behind an interface chosen by per-client config:
`LLMProvider`, `STTProvider`, `TTSProvider`, `InventoryAdapter`, `Repository` (storage), `WhatsAppClient`, `Clock`.
Every interface has a Mock implementation, so the full test suite runs offline with no keys.

---

## 2. Folder structure

```
.
├── PLAN.md / README.md / pyproject.toml / .env.example / .gitignore
├── clients/
│   └── truechoice/
│       ├── client.yaml            # client_id, phone_number_id, secret names, providers,
│       │                          # staff numbers, languages, voice policy, inventory adapter
│       ├── business_rules.yaml    # hours, EMI rates, alert caps, near-match rules, opt-out words…
│       └── inventory_mock.json    # 30 realistic cars
├── app/
│   ├── main.py                    # FastAPI app factory, mounts routers
│   ├── settings.py                # env/.env loading (pydantic-settings), secret resolution
│   ├── core/
│   │   ├── client_config.py       # load + validate client.yaml / business_rules.yaml
│   │   ├── phone.py               # normalise to +91 E.164 (phonenumbers lib)
│   │   ├── clock.py               # injectable clock, IST helpers, send-window check
│   │   ├── language.py            # script detection (Malayalam / Latin), opt-out + yes/no words
│   │   └── money.py               # parse "8L", "8 lakh", "800000"; INR formatting; numbers→Malayalam words
│   ├── storage/
│   │   ├── models.py              # dataclasses: Customer, Message, Lead, Requirement, AlertLog…
│   │   ├── repository.py          # Repository Protocol (the interface)
│   │   ├── sqlite_repo.py         # local
│   │   └── dynamo_repo.py         # AWS (day 3)
│   ├── inventory/
│   │   ├── base.py                # InventoryAdapter: search_cars(), list_all_cars(), get_car()
│   │   ├── mock_adapter.py
│   │   └── real_adapter.py        # stub, raises NotImplementedError with a clear message
│   ├── providers/
│   │   ├── llm/   base.py, openai_compatible.py (DeepSeek/OpenRouter/Gemini), anthropic.py,
│   │   │          mock.py, fallback.py (tries chain in order)
│   │   ├── stt/   base.py, sarvam.py (Saarika), mock.py
│   │   └── tts/   base.py, sarvam.py (Bulbul), mock.py, audio.py (ffmpeg → OGG/Opus)
│   ├── whatsapp/
│   │   ├── client.py              # send text / audio / template, upload + download media
│   │   ├── webhook.py             # GET verify, POST receive, X-Hub-Signature-256 check
│   │   └── parse.py               # Graph payload → InboundMessage
│   ├── agent/
│   │   ├── prompts.py             # system prompt (identity, rules, language mirroring)
│   │   ├── tools.py               # tool schemas + implementations
│   │   ├── agent.py               # tool-calling loop → AgentReply{text, spoken_text}
│   │   ├── guardrails.py          # post-checks: no un-sourced prices/years, no promises
│   │   └── confirmation.py        # voice read-back: hold save_* until customer confirms
│   ├── conversation/
│   │   ├── router.py              # the MessageRouter above
│   │   ├── voice_in.py            # download → duration check → STT → delete audio
│   │   └── voice_out.py           # mirror policy, phrase cache, char cap, 8 s timeout
│   ├── leads/
│   │   ├── parse.py               # LEAD <phone> <text> → phone + structured requirement (LLM + rules)
│   │   ├── service.py             # create/update, consent YES, opt-out/in across a customer's leads
│   │   └── __main__.py            # python -m app.leads --client truechoice (masked list)
│   ├── handoff/
│   │   ├── handoff.py             # pause bot, staff summary, customer notice
│   │   └── staff_commands.py      # RESUME +91…, LEAD <phone> <free text>
│   ├── alerts/
│   │   ├── matcher.py             # pure functions: exact / near match + scoring
│   │   ├── engine.py              # the daily run (all rules in §5)
│   │   └── cli.py                 # python -m app.alerts.cli --client truechoice --dry-run
│   ├── dashboard/
│   │   ├── auth.py                # per-client password (scrypt hash), signed session cookie, CSRF
│   │   ├── routes.py              # leads, conversations, wishlists, alerts, stats
│   │   ├── templates/*.html       # Jinja2 + HTMX, mobile-first CSS (no build step)
│   │   └── static/
│   ├── cli_chat.py                # talk to the agent in the terminal, no WhatsApp needed
│   └── lambda_handlers.py         # Mangum API handler + alert job handler (day 3)
├── infra/
│   └── template.yaml              # SAM: API GW, Lambdas, DynamoDB, EventBridge, Secrets, ffmpeg layer
├── scripts/
│   ├── seed_dev.py                # create local DB, dashboard password, sample data
│   └── live_eval.py               # optional: run guardrail scenarios against real DeepSeek
└── tests/
    ├── conftest.py                # fixtures: mock providers, temp SQLite, frozen clock
    ├── unit/ …                    # phone, money, language, matcher, config, each provider
    └── scenarios/ …               # end-to-end flows with MockLLM + mock WhatsApp
```

---

## 3. Key design decisions

| Topic | Decision |
|---|---|
| Webhook latency | Meta retries if we don't answer quickly. Local: FastAPI `BackgroundTasks`. AWS: API Lambda stores the inbound message and async-invokes a worker Lambda, then returns 200. |
| Idempotency | WhatsApp message ids are stored; a duplicate delivery is ignored. |
| Anti-hallucination | (1) System prompt: only facts from `search_cars`. (2) Guardrail post-check: any ₹/lakh amount, year or KM figure in the reply must appear in tool results from this conversation, and words like "discount / guaranteed / approved / ഉറപ്പ്" trigger a rewrite. If the rewrite still fails, a safe fallback reply is sent. |
| Off-topic | Prompt rule plus scenario tests (news, coding, other dealers, trivia → polite refusal and a turn back to cars). |
| Language | Reply in the customer's language **and script**: Malayalam script → Malayalam; Latin-script Malayalam → Manglish; English → English. The detected language is stored per customer. |
| spoken_text | Generated only when a voice reply will be sent (saves tokens). The LLM produces short Malayalam-script text; a validator converts any leftover digits with `money.py` (e.g. 5,50,000 → "അഞ്ചര ലക്ഷം"). |
| Voice confirmation | When the turn came from voice, `save_lead` / `save_requirement` don't write. They return `needs_confirmation` with a read-back, and the pending action is stored. The next "yes / sheri / athe / ok" commits it; anything else discards it or updates it. |
| TTS cache | Fixed phrases (greeting, "please repeat", "salesperson will contact you") are cached as **WhatsApp media ids** (valid about 30 days), so no S3 is needed. |
| Templates | Alerts and the "still looking?" message are templates (required outside the 24 h window). Template parameters can't contain newlines, so the grouped alert uses one parameter per car (3 slots, blank-filled). |
| First alert run | The first run for a client only seeds `seen_cars` (no sends). Otherwise all 30 existing cars would be "new". |
| Availability re-check | `InventoryAdapter.get_car(id)` is added (the default implementation filters `list_all_cars()`) and called right before each send. |
| Time | All rules use an injected `Clock`, so tests freeze time. The cron runs at 17:00 IST, but the engine still enforces the 09:00–20:00 window (late or manual runs defer). |
| Secrets | `.env` locally. On AWS, `client.yaml` names secrets that are fetched from Secrets Manager and cached per Lambda container. |
| Python | Code targets 3.12 (the Lambda runtime). Your machine has 3.13, which is fine for local dev (no 3.13-only syntax). |

---

## 4. Data model (same Repository interface for SQLite and DynamoDB)

All records are keyed by `client_id` first (tenant isolation).

- **customers**: phone (+91…), name, language, consent (bool, timestamp, source), opted_out_at, bot_paused, last_inbound_at
- **messages**: id (wa message id), phone, direction, text, source (`text|voice|template|staff`), alert_id (context), ts
- **pending_actions**: phone, tool, args, created_at (voice confirmation)
- **leads**: id, phone, name, interest, budget, notes, status `new|contacted|closed`, created_at
- **requirements**: id, phone, budget_max, min_year, make, model, fuel, max_km, status `active|fulfilled|expired|deleted`, unanswered_alerts, still_looking_sent_at, last_near_match_at, created_at, expires_at (+60 d)
- **seen_cars**: car_id, first_seen_at
- **alerts_log**: id, phone, requirement_id, car_ids, kind `match|near|still_looking`, status `sent|dry_run|skipped:<reason>|failed`, ts
- **counters**: (date, name) → value: daily sends, daily TTS characters
- **tts_cache**: phrase_hash → media_id, expires_at
- **processed_events**: wa message ids (dedupe, TTL 7 d)

---

## 5. Alert engine rules (the order they're applied)

1. Load active requirements. Expire those past 60 days.
2. `list_all_cars()` → new = not in `seen_cars` (first run: seed only). **If it errors or returns 0 cars: abort, raise an alarm, mark nothing seen.**
3. For each requirement: the customer must have consent, not be opted out, and not have the bot paused (assumption, see Q7).
4. Match: budget, min year, make/model (case/alias-insensitive), fuel, max KM. Near match = at most 10 % over budget **or** 1 year older (not both), labelled, at most 1 per customer per 7 days.
5. Drop cars already alerted to this customer (any requirement).
6. If `unanswered_alerts >= 3`: send one "still looking?" (if not already sent), then expire on the next run if there is still no reply.
7. Re-check `get_car()` availability. Top 3 by score (exact before near, then closest price, then newest) → write `alerts_log` rows as `pending` (unique per client + phone + car) → send one template → mark `sent` / `failed`. A retry that finds an existing row skips that car.
8. Enforce the send window and the global daily cap (remaining customers are deferred to the next run).
9. `--dry-run`: run every check, write `dry_run` rows to the alert log, send nothing.
10. Mark the new cars seen **after** processing.
Any inbound customer message resets `unanswered_alerts`. If it came within 72 h of an alert, the alert is injected into the agent context.

---

## 6. Build order (each step ends with "how to run / test / next")

**Day 1: core and text conversations**
1. Scaffold: pyproject, settings, client config loader, phone/money/language/clock utils, plus tests.
2. Storage: models, Repository protocol, SQLite implementation, plus tests.
3. Inventory: the 30-car mock JSON, mock adapter, real stub, plus tests.
4. LLM providers: OpenAI-compatible, Anthropic (prompt caching), Mock, fallback chain, plus tests (HTTP mocked with `respx`).
5. Agent: prompt, tools, EMI, guardrails, tool loop, plus scenario tests. **`cli_chat.py` lets you chat with real DeepSeek in the terminal.**
6. WhatsApp: client, webhook, signature check, dedupe, router (text only). Test on your real test number through a tunnel (cloudflared/ngrok).

**Day 2: voice, handoff, alerts, DynamoDB**

7. Voice in: verify Sarvam's real API limits and response fields first; then STT, media download, 60 s limit, low-confidence handling, read-back confirmation.
8. Voice out: spoken_text, Sarvam TTS, ffmpeg OGG/Opus, mirror policy, cache, cap, 8 s fallback.
9. Handoff and staff commands (RESUME / LEAD), opt-out detection and confirmation, auto-resume after 24 h, yard-visitor leads with customer consent.
10. Alert engine plus the dry-run CLI.
11. (evening) DynamoDB: write the access-pattern table first, then `dynamo_repo.py`, tested with **moto** against the same repository test suite as SQLite.

**Day 3: dashboard and AWS**

12. Dashboard: login, Leads, Conversations (pause/resume), Wishlists (add walk-in with a mandatory consent checkbox / fulfil / delete), Alerts log, Today's stats.
13. SAM template (worker Lambda timeout 120 s), Lambda handlers, Secrets Manager, ffmpeg layer → `sam deploy`, point the Meta webhook at API Gateway, smoke test.
14. README final pass.

**Cut order if time is short:** dashboard pages beyond Leads / Conversations / Wishlists → voice out → voice in.

---

## 7. Do these TODAY (outside the code, long lead time)

- **Submit WhatsApp templates to Meta now.** Approval can take hours to a day. I'll draft the exact text in step 1: `car_alert_v1` (3 car slots), `car_near_match_v1`, `still_looking_v1`, and possibly `staff_lead_alert_v1` (see Q2), each in ml + en.
- Meta app: get the test phone number, phone_number_id, permanent system-user token and app secret.
- Sarvam API key. DeepSeek API key (plus any fallback keys you want).
- Install ffmpeg locally: `winget install Gyan.FFmpeg`. It isn't installed on this machine yet.

---

## 8. Open questions (my default in **bold**; reply "defaults OK" or change any)

1. **Consent:** how does a chat customer give consent for alerts? **The agent asks explicitly when saving a requirement ("Shall we WhatsApp you when a matching car arrives?"), and the yes is stored with a timestamp.** For staff `LEAD` commands (formerly `WISH`): ~~the staff member's command counts as recorded verbal consent~~ **superseded on 2026-10-20: the customer must reply YES to a confirmation message (see §9).**
2. **Staff notifications outside 24 h:** WhatsApp only allows free-form messages to staff numbers that messaged the bot in the last 24 h. **Use a `staff_lead_alert` template for handoff summaries.** OK?
3. **Bot pause:** does a paused bot auto-resume? **Auto-resume after 48 h of no staff activity, plus manual resume.**
4. **Business hours for handoff wording:** **9:30–19:00 Mon–Sat; outside those hours the customer is told "tomorrow morning".** Real hours?
5. **EMI assumptions:** **10.5 %–14 % p.a., 12–60 months, shown as an indicative range with a disclaimer.** Real numbers from True Choice?
6. **STT confidence:** Sarvam may not return a confidence score. **Treat as low confidence if the transcript is empty, under 2 words, or mostly non-speech tokens.** I'll check the actual API response in step 7. Also, Sarvam's real-time endpoint likely caps audio at about 30 s, so **audio of 30–60 s is split into chunks.**
7. **Alerts to paused customers:** **skip while the bot is paused (a salesperson is already handling them).**
8. **Global daily send cap:** **200/day per client (well under Meta's tier-1 limit of 1k unique users/day).** And the daily TTS character cap: **20,000 chars/day.**
9. **Opt-out scope:** does "stop" stop only alerts or the bot too? **It stops alerts and marketing. The customer can still chat, and the bot confirms once: "You won't receive alerts. Reply START to resume."**
10. **Dashboard users:** **one shared password per client (from Secrets Manager).** Individual staff logins can come later.
11. **Anthropic model:** **`claude-haiku-5-5`**, configurable. Fallback chain: **DeepSeek → OpenRouter → Anthropic.** Gemini is supported but not in the chain by default.
12. **AWS region:** **ap-south-1 (Mumbai)** for latency and India data residency.
13. **Alias handling:** customers say "Swift", "Innova Crysta", "XUV 7OO". **A small alias map in business_rules.yaml.**
14. **Real inventory API:** do you have docs yet? If not, the stub stays and the mock JSON is used through day 3.

---

## 9. Decisions and plan changes (agreed 2026-10-16). These override anything above.

- **Q1:** defaults. The dashboard wishlist form has a **mandatory** "customer agreed to WhatsApp updates" checkbox, and `consent_recorded_by` stores who recorded it.
- **Q2:** `staff_lead_alert` as a Utility template. **Q3:** auto-resume after **24 h**.
- **Q4/Q5:** defaults, kept in `business_rules.yaml`. **Q6:** verify Sarvam's real limits and response fields in step 7.
- **Q7, Q8, Q10, Q12, Q13:** defaults.
- **Q9:** the opt-out confirmation states exactly what was stopped, in the customer's language.
- **Q11:** no Claude model name in code or committed config. `ANTHROPIC_MODEL` is filled in by you in `.env`. Chain: DeepSeek → OpenRouter → Anthropic.
- **Q14:** mock adapter only for now.
- **Alerts:** abort and raise an alarm (error log / CloudWatch metric) if `list_all_cars()` fails or returns 0 cars. Never mark cars seen in that case.
- **Alert idempotency:** write the `alerts_log` row as `pending` with unique key `(client_id, phone, car_id)` *before* sending, then mark it `sent` / `failed`.
- **Guardrails** understand Malayalam number words and Manglish amounts ("ettu lakh", "അഞ്ചര ലക്ഷം", "8L"). The safe fallback is "let me check with our team" plus a handoff. Tests are required.
- **Retention:** `retention_days` (default 90) in business rules, plus a `delete_customer_data(phone)` function.
- **Phones:** Indian numbers → `+91…`. Valid foreign numbers (Gulf NRIs are common in Kerala) are kept in E.164 with their own country code, not rejected.
- **Templates:** near matches are labelled inside the car line, so no separate near-match template. Because parameters can't contain newlines, there is one alert template per match count (1/2/3 cars). Drafts are in `docs/whatsapp_templates.md`.
- **Staff command `WISH` is renamed `LEAD` (2026-10-20).** Staff commands are `RESUME` and `LEAD`.
- **A "lead" is a customer who visited the yard, didn't find the car they wanted, and wants a WhatsApp alert when a matching car arrives (2026-10-20).** Staff create it with `LEAD <phone> <free text>`. It is stored as `pending_consent` and only becomes `active` after the customer replies YES to one confirmation message; STOP opts them out of all their leads.
  - Lead status (system): `pending_consent`, `active`, `notified`, `expired`, `opted_out`. Stage (staff pipeline): `new`, `interested`, `visit_booked`, `sold`, `lost`. Default expiry is 90 days (`leads.expiry_days` in client.yaml).
  - The chat customer's visit / test-drive request, previously also called a "lead", is now an **enquiry** (`Enquiry`, table `enquiries`, bot tool `save_enquiry`).

