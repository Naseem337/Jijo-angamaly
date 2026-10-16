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
| **5 Agent: prompt, tools, EMI, guardrails, tool loop, `cli_chat.py`** | **NEXT** |
| 6–14 | Not started |

The user chose to build the **AI brain first (Steps 4 → 5)**, then WhatsApp (Step 6).
203 tests pass.

## External setup done by the user

- **Meta / WhatsApp test number** works: +1 555 151 2035, Phone number ID `1414805245056988`.
  - A template and a free-form text were both received on the user's phone.
  - The permanent system-user token (`whatsapp-bot`, never expires) is in `.env` and was verified
    against Graph API **v25.0**. The config was moved from v21.0 to v25.0.
  - App secret (32 hex characters) and staff number are in `.env`.
  - The verify token was generated into `.env`.
  - **Webhook not configured yet.** That happens in Step 6, via a cloudflared tunnel to
    `/webhook/truechoice`.
- `scripts/send_test_message.ps1` sends a test text from the test number.
- **AI keys: none yet.** `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` and
  `ANTHROPIC_MODEL` are all empty. The user was in the middle of creating a DeepSeek account
  (platform.deepseek.com).
  - Next user steps: add a small amount of credit ($2–5), create the API key, paste it into `.env`.
  - Then verify it with `python -m app.providers.llm --client truechoice --each`.
- WhatsApp templates (docs/whatsapp_templates.md) have **not** been submitted yet. They're only
  needed for alerts (Step 10).

## Decisions made in chat (not yet in PLAN.md)

- **Database:** SQLite locally, DynamoDB on AWS (Step 11). The user asked about Supabase. The reasons
  to keep DynamoDB: one AWS account, no server, near-zero cost, and Supabase free projects pause when
  idle. Switching would mean a Postgres repository. Undecided only if the user raises it again.
- **Inventory management UI:** the staff dashboard (Step 12) covers leads, chats, wishlists and alerts.
  Whether it also needs an Inventory page depends on where True Choice keeps its stock:
  - an API means no page is needed;
  - Excel or Google Sheets means reading the sheet;
  - nothing means adding an Inventory page to the dashboard.
  **We are waiting on the user to ask True Choice.**
- `purge_old_data` deletes old `pending_actions` (the interface docstring wins). The test was fixed
  to match.
- The anthropic SDK is 1.x (uses httpx2, so respx can't mock it). Its tests use a fake client object.
  OpenAI-compatible providers use httpx and are tested with respx.
