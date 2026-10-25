"""Point Meta's WhatsApp webhook at a fixed address (the Railway URL).

    .venv\\Scripts\\python.exe scripts\\set_webhook.py https://your-app.up.railway.app

Do this once after deploying, and again only if the address changes. Stop the local bot
(scripts\\dev_whatsapp.py) first: it re-points Meta at your laptop every time it starts.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from app.core.client_config import load_client_config
from app.settings import EnvSecretResolver, get_settings
from dev_whatsapp import point_meta_at  # same folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("base_url", help="e.g. https://your-app.up.railway.app")
    parser.add_argument("--client", default="truechoice")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    base = args.base_url.strip().rstrip("/")
    if not base.startswith("https://"):
        print("The address must start with https://")
        return 2
    try:
        health = httpx.get(f"{base}/health", timeout=20)
    except httpx.HTTPError as exc:
        print(f"Could not reach {base}/health: {exc!r}. Is the Railway deploy finished?")
        return 1
    if health.status_code != 200 or '"status":"ok"' not in health.text.replace(" ", ""):
        print(f"{base}/health answered {health.status_code}: {health.text[:150]}")
        return 1
    print(f"Server is up: {health.text.strip()}")

    config = load_client_config(args.client, get_settings().clients_dir)
    secrets, wa = EnvSecretResolver(), config.whatsapp
    point_meta_at(f"{base}/webhook/{args.client}", secrets.get(wa.access_token_secret),
                  secrets.get(wa.app_secret_secret), secrets.get(wa.verify_token_secret))
    print(f"Meta now sends WhatsApp messages to {base}/webhook/{args.client}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
