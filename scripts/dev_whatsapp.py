"""Run the bot locally and connect it to WhatsApp in one go.

    .venv\\Scripts\\python.exe scripts\\dev_whatsapp.py            (client truechoice)

1. starts the web server (uvicorn, port 8000)
2. starts a free Cloudflare tunnel, which gives the server a public https address
3. tells Meta to send this client's WhatsApp messages to <tunnel>/webhook/<client>
4. keeps running until you press Ctrl+C, then stops both

The tunnel address changes on every start, which is why step 3 is automatic. Meta's app
id is looked up from the access token; the app secret signs the request (both in .env).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.client_config import load_client_config  # noqa: E402
from app.settings import EnvSecretResolver, get_settings  # noqa: E402

GRAPH = "https://graph.facebook.com/v25.0"
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_cloudflared() -> str:
    for candidate in (shutil.which("cloudflared"), Path.home() / "tools" / "cloudflared.exe",
                      Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
                      Path(r"C:\Program Files\cloudflared\cloudflared.exe")):
        if candidate and Path(candidate).exists():
            return str(candidate)
    sys.exit("cloudflared not found. Download it to %USERPROFILE%\\tools\\cloudflared.exe "
             "(https://github.com/cloudflare/cloudflared/releases).")


def wait_for(check, what: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(1)
    sys.exit(f"Timed out waiting for {what}.")


def server_up(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def public_dns_ready(host: str) -> bool:
    """Meta resolves the tunnel with public DNS; wait until it exists there."""
    try:
        answer = httpx.get("https://cloudflare-dns.com/dns-query", params={"name": host, "type": "A"},
                           headers={"accept": "application/dns-json"}, timeout=10).json()
    except (httpx.HTTPError, ValueError):
        return False
    return any(a.get("type") == 1 for a in answer.get("Answer", []))


def point_meta_at(callback_url: str, token: str, app_secret: str, verify_token: str) -> None:
    app_id = httpx.get(f"{GRAPH}/debug_token", params={"input_token": token, "access_token": token},
                       timeout=20).json().get("data", {}).get("app_id")
    if not app_id:
        sys.exit("Could not find the Meta app for the WhatsApp token in .env.")
    app_token = f"{app_id}|{app_secret}"
    current = httpx.get(f"{GRAPH}/{app_id}/subscriptions", params={"access_token": app_token}, timeout=20).json()
    fields = {f["name"] for sub in current.get("data", []) if sub.get("object") == "whatsapp_business_account"
              for f in sub.get("fields", [])} | {"messages"}  # keep whatever was already subscribed
    for attempt in range(1, 6):  # Meta calls the URL to verify it; a brand-new tunnel can need a moment
        resp = httpx.post(f"{GRAPH}/{app_id}/subscriptions", timeout=30, data={
            "object": "whatsapp_business_account", "callback_url": callback_url,
            "verify_token": verify_token, "fields": ",".join(sorted(fields)), "access_token": app_token,
        })
        if resp.status_code == 200 and resp.json().get("success"):
            return
        print(f"  Meta could not verify the address yet (attempt {attempt}): "
              f"{resp.json().get('error', {}).get('message', resp.text[:200])}")
        time.sleep(5)
    sys.exit("Meta would not accept the webhook address. Is the server running?")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--client", default="truechoice")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    config = load_client_config(args.client, get_settings().clients_dir)
    secrets = EnvSecretResolver()
    wa = config.whatsapp
    token, app_secret = secrets.get(wa.access_token_secret), secrets.get(wa.app_secret_secret)
    verify_token = secrets.get(wa.verify_token_secret)
    if server_up(args.port):
        sys.exit(f"Something is already running on port {args.port}. Close it first (or use --port).")

    print("1/3 Starting the bot server…")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(args.port)], cwd=ROOT)
    tunnel = None
    try:
        wait_for(lambda: server_up(args.port), "the bot server")
        print("2/3 Opening the Cloudflare tunnel…")
        tunnel = subprocess.Popen([find_cloudflared(), "tunnel", "--no-autoupdate", "--url",
                                   f"http://localhost:{args.port}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        found: list[str] = []
        registered = threading.Event()

        def read_tunnel_log() -> None:  # keep draining the pipe so cloudflared never blocks
            for line in tunnel.stdout:
                if not found and (match := TUNNEL_RE.search(line)):
                    found.append(match.group(0))
                if "Registered tunnel connection" in line:
                    registered.set()

        threading.Thread(target=read_tunnel_log, daemon=True).start()
        wait_for(lambda: bool(found) and registered.is_set(), "the tunnel")
        base = found[0]
        wait_for(lambda: public_dns_ready(base.removeprefix("https://")), "the tunnel address to go live")

        callback = f"{base}/webhook/{args.client}"
        print(f"3/3 Pointing Meta's webhook at {callback} …")
        point_meta_at(callback, token, app_secret, verify_token)
        print(f"\n✅ The bot is live on WhatsApp for '{args.client}'. Message the test number from your phone.")
        print("   Keep this window open and the laptop awake. Press Ctrl+C to stop.\n")
        while server.poll() is None and tunnel.poll() is None:
            time.sleep(1)
        print("The server or the tunnel stopped unexpectedly.")
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
