"""Point the resort bot's Meta (WhatsApp) webhook at its Render address.

    .venv\Scripts\python.exe scripts\set_resort_webhook.py https://resort-bot.onrender.com

Do this once after deploying, and again only if the address changes. Stop scripts\dev_resort.py first: it re-points
Meta at your laptop every time it starts. Meta checks the address with the verify token, so this also proves the
verify token on Render matches the one in .env.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.settings import load_dotenv  # noqa: E402
from dev_whatsapp import point_meta_at  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        print("Usage: set_resort_webhook.py https://your-service.onrender.com")
        return 2
    base = sys.argv[1].strip().rstrip("/")
    load_dotenv()
    need = ["RESORT_WHATSAPP_ACCESS_TOKEN", "RESORT_WHATSAPP_APP_SECRET", "RESORT_WHATSAPP_VERIFY_TOKEN"]
    if missing := [k for k in need if not os.environ.get(k)]:
        print(f"Fill these in .env first: {', '.join(missing)}")
        return 2
    try:  # a sleeping free instance needs about a minute to wake up
        health = httpx.get(f"{base}/health", timeout=120)
    except httpx.HTTPError as exc:
        print(f"Could not reach {base}/health: {exc!r}")
        return 1
    if health.status_code != 200:
        print(f"{base}/health answered {health.status_code}")
        return 1
    print("Server is up.")
    point_meta_at(f"{base}/resort/webhook", os.environ["RESORT_WHATSAPP_ACCESS_TOKEN"],
                  os.environ["RESORT_WHATSAPP_APP_SECRET"], os.environ["RESORT_WHATSAPP_VERIFY_TOKEN"])
    print(f"Meta now sends the resort bot's WhatsApp messages to {base}/resort/webhook")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
