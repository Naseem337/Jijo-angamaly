"""Run the resort bot locally and connect it to WhatsApp in one go.

    .venv\Scripts\python.exe scripts\dev_resort.py

Same idea as dev_whatsapp.py: starts the server and a free Cloudflare tunnel, then points the
Resort Bot Meta app's webhook at <tunnel>/resort/webhook. The tunnel address changes on every start.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.settings import load_dotenv  # noqa: E402
import dev_whatsapp as dw  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    need = ["RESORT_WHATSAPP_ACCESS_TOKEN", "RESORT_WHATSAPP_APP_SECRET", "RESORT_WHATSAPP_VERIFY_TOKEN",
            "RESORT_WHATSAPP_PHONE_NUMBER_ID"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        sys.exit(f"Fill these in .env first: {', '.join(missing)}")
    port = 8000
    if dw.server_up(port):
        sys.exit("Something is already running on port 8000. Close it first.")
    print("1/3 Starting the server…")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT)
    tunnel = None
    try:
        dw.wait_for(lambda: dw.server_up(port), "the server")
        print("2/3 Opening the Cloudflare tunnel…")
        tunnel = subprocess.Popen([dw.find_cloudflared(), "tunnel", "--no-autoupdate", "--url",
                                   f"http://localhost:{port}"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, errors="replace")
        found: list[str] = []
        registered = threading.Event()

        def read_log() -> None:
            for line in tunnel.stdout:
                if not found and (m := dw.TUNNEL_RE.search(line)):
                    found.append(m.group(0))
                if "Registered tunnel connection" in line:
                    registered.set()

        threading.Thread(target=read_log, daemon=True).start()
        dw.wait_for(lambda: bool(found) and registered.is_set(), "the tunnel")
        base = found[0]
        dw.wait_for(lambda: dw.public_dns_ready(base.removeprefix("https://")), "the tunnel address to go live")
        callback = f"{base}/resort/webhook"
        print(f"3/3 Pointing Meta's webhook at {callback} …")
        dw.point_meta_at(callback, os.environ["RESORT_WHATSAPP_ACCESS_TOKEN"], os.environ["RESORT_WHATSAPP_APP_SECRET"],
                         os.environ["RESORT_WHATSAPP_VERIFY_TOKEN"])
        print("\n✅ The resort bot is live. Message the test number from an added phone.")
        print(f"   Dashboard: http://localhost:{port}/resort/admin/   (keep this window open; Ctrl+C stops)\n")
        while server.poll() is None and tunnel.poll() is None:
            time.sleep(1)
        return 1
    except KeyboardInterrupt:
        print("\nStopping…")
        return 0
    finally:
        for proc in (tunnel, server):
            if proc and proc.poll() is None:
                proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
