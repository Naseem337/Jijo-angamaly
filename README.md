# Dealer WhatsApp Agent

A multi-client WhatsApp AI agent for pre-owned car dealerships. The first client is **True Choice**
(Kerala). It handles Malayalam, English and Manglish, in text and voice.
See [PLAN.md](PLAN.md) for the architecture, build order and decisions.

## Status

| Step | What | State |
|---|---|---|
| 1 | Scaffold: settings, client config, phone/money/language/clock utils | ✅ |
| 2 | Storage (SQLite) | ⏳ |
| 3 | Inventory adapter (mock, 30 cars) | ⏳ |
| … | see PLAN.md §6 | |

## Setup (Windows PowerShell)

```powershell
cd "C:\Users\hp\Downloads\Msquare\WHatsapp automation"
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -e ".[dev]"
Copy-Item .env.example .env            # then fill in values as you get them
```

You'll also need ffmpeg (for voice replies, step 8): `winget install Gyan.FFmpeg`, then open a new terminal.

## Run tests

```powershell
python -m pytest                        # everything
python -m pytest tests\unit\test_money.py -v   # one file, verbose
```

The tests need no API keys and no network access.

## Run the server

```powershell
python -m uvicorn app.main:app --reload --port 8000
```
Then open http://127.0.0.1:8000/health. It should return `{"status":"ok", "clients":["truechoice"], ...}`.

## Layout

- `app/core/`: pure helpers: config loader, phone normalisation, IST clock, money parsing/speaking, language and opt-out detection
- `clients/<client_id>/`: per-client `client.yaml` + `business_rules.yaml` (and mock inventory)
- `docs/whatsapp_templates.md`: template texts to submit to Meta
- `tests/`: pytest suite

## Configuration and secrets

- `clients/<id>/client.yaml` may use `${ENV_VAR}` or `${ENV_VAR:-default}`.
- `*_secret` fields hold secret **names**. Locally, a name maps to an env var:
  `truechoice/whatsapp_token` → `TRUECHOICE_WHATSAPP_TOKEN`. On AWS, the name maps to a Secrets Manager secret.
- The Claude model ID is never hardcoded. Set `ANTHROPIC_MODEL` in `.env`.
