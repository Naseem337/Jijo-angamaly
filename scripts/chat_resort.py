"""Chat with the resort bot in the terminal, using the real AI from .env (nothing is sent on WhatsApp).

    .venv\\Scripts\\python.exe scripts\\chat_resort.py            the old bot
    .venv\\Scripts\\python.exe scripts\\chat_resort.py --agent    the new AI-first agent (app/resort/agent/)

Type like a guest. Commands:  /paid  = pretend the payment came in,  /reset = new guest and empty database,  /quit.
Uses a fresh temporary database with the seed data, so it never touches real bookings.
"""

from __future__ import annotations

import itertools
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ.setdefault("RESORT_STAFF_NUMBERS", "")
from app.core.clock import SystemClock  # noqa: E402
from app.providers.llm import build_llm  # noqa: E402
from app.resort import seed  # noqa: E402
from app.resort.config import load_resort_config  # noqa: E402
from app.resort.db import ResortRepo  # noqa: E402
from app.resort.flow import BookingBot  # noqa: E402
from app.resort.payments import MockPayments  # noqa: E402
from app.settings import EnvSecretResolver  # noqa: E402
from app.whatsapp.parse import InboundMessage  # noqa: E402


class PrintWA:
    """Sends nothing: prints what the guest (or staff) would get."""

    def __init__(self, guest: str = "") -> None:
        self.guest = guest

    def send_text(self, to, text):
        who = "BOT" if not self.guest or to == self.guest else f"STAFF ALERT to {to}"
        print(f"\n{who}:\n" + "\n".join("    " + ln for ln in text.split("\n")))
        return "wamid.x"

    def send_image(self, to, media_id, caption=""):
        print(f"BOT: [photo {media_id}]")
        return "wamid.img"

    def upload_media(self, data, mime_type="image/jpeg", filename="x"):
        return f"media:{filename}"

    def mark_read(self, *a, **k):
        return True

    def send_template(self, *a, **k):
        return "wamid.x"

    def download_media(self, *a, **k):
        raise RuntimeError("voice notes are not supported in this terminal chat")


PHONE = "+919847000123"


def new_session(agent: bool = False):
    """A fresh scratch database with the seed data. agent=True: AGENT_MODE=agent (the gate, the AI agent, the guard
    and the outbox, exactly as live), but WhatsApp is the printing fake and payments are fake links."""
    repo = ResortRepo(Path(tempfile.mkdtemp()) / "r.sqlite3")
    seed.seed(repo)
    cfg = load_resort_config()
    if agent:
        cfg = cfg.model_copy(update={"agent_mode": "agent"})
    llm = build_llm(cfg, EnvSecretResolver())
    bot = BookingBot(cfg, repo, PrintWA(PHONE), MockPayments(), SystemClock(), llm=lambda: llm)
    return repo, bot


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    agent = "--agent" in sys.argv[1:]
    repo, bot = new_session(agent)
    phone, ids = PHONE, itertools.count(1)
    print(f"Resort bot chat ({'NEW AGENT' if agent else 'old bot'}). /paid = payment received, /reset = start over, "
          "/quit = exit. Nothing is sent on WhatsApp; payment links are fake.")
    while True:
        try:
            text = input("\nYOU: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/reset":
            repo, bot = new_session(agent)
            print("(new empty database)")
            continue
        if text == "/paid":
            booking = next(iter(repo.list_bookings(phone=phone, status="hold", limit=1)), None)
            if booking:
                bot.mark_paid(booking_id=booking.id, payment_id="pay_test")
            else:
                print("(no booking on hold)")
            continue
        outcome = bot.handle(InboundMessage(f"wamid.cli{next(ids)}", phone, "text", datetime.now(), text=text))
        if outcome == "ai_down":
            print("(the AI did not answer: check the keys in .env)")


if __name__ == "__main__":
    main()