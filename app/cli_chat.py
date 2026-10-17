"""Chat with the agent in the terminal, as a customer would on WhatsApp.

    python -m app.cli_chat --client truechoice
    python -m app.cli_chat --client truechoice --voice      # pretend messages are voice notes
    python -m app.cli_chat --client truechoice --fresh      # forget this test customer first

Uses the real LLM keys from .env and a separate local database (data/cli_chat.sqlite3).
Type /reset to start over, /quit to leave.
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.agent import Agent
from app.core.client_config import load_client_config
from app.core.clock import SystemClock
from app.core.phone import normalize_phone
from app.inventory import build_inventory_adapter
from app.providers.llm import LLMNotConfigured, build_llm
from app.settings import PROJECT_ROOT, EnvSecretResolver, get_settings
from app.storage.sqlite_repo import SQLiteRepository

DIM, RESET, GREEN, YELLOW = "\033[2m", "\033[0m", "\033[32m", "\033[33m"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli_chat")
    parser.add_argument("--client", required=True)
    parser.add_argument("--phone", default="+919000000001", help="the pretend customer's number")
    parser.add_argument("--voice", action="store_true", help="treat every message as a voice note")
    parser.add_argument("--fresh", action="store_true", help="forget this customer before starting")
    parser.add_argument("--debug", action="store_true", help="show provider/tool logs")
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO if args.debug else logging.ERROR,
                        format="%(levelname)s %(name)s: %(message)s")

    config = load_client_config(args.client, get_settings().clients_dir)
    try:
        llm = build_llm(config, EnvSecretResolver())
    except LLMNotConfigured as exc:
        print(f"No AI provider is set up:\n{exc}\nAdd an API key to .env (see .env.example).")
        return 1
    db_path = PROJECT_ROOT / "data" / "cli_chat.sqlite3"
    db_path.parent.mkdir(exist_ok=True)
    repo = SQLiteRepository(db_path)
    phone = normalize_phone(args.phone)
    if args.fresh:
        repo.delete_customer_data(config.client_id, phone)
    agent = Agent(config, llm, repo, build_inventory_adapter(config), SystemClock())

    print(f"Chatting with {config.display_name} as {phone}"
          f"{' (voice mode)' if args.voice else ''}. /reset to start over, /quit to leave.\n")
    while True:
        try:
            text = input(f"{GREEN}You:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in ("/quit", "/exit"):
            return 0
        if text == "/reset":
            repo.delete_customer_data(config.client_id, phone)
            print(f"{DIM}(conversation forgotten){RESET}\n")
            continue

        reply = agent.handle(phone, text, source="voice" if args.voice else "text")
        print(f"{YELLOW}Bot:{RESET} {reply.text}")
        info = [f"via {reply.provider or '-'}", f"lang {reply.language}"]
        if reply.tools_used:
            info.append("tools: " + ", ".join(reply.tools_used))
        if reply.guardrail != "ok":
            info.append(f"guardrail: {reply.guardrail} {reply.problems}")
        if reply.saved:
            info.append("saved: " + ", ".join(reply.saved))
        if reply.handoff:
            info.append(f"HANDOFF to staff: {reply.handoff.reason}")
        print(f"{DIM}  [{' | '.join(info)}]{RESET}\n")


if __name__ == "__main__":
    raise SystemExit(main())
