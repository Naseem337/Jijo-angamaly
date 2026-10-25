"""Make a Railway-ready copy of .env, so you can paste it into Railway's Variables editor.

    .venv\\Scripts\\python.exe scripts\\railway_env.py

Writes data\\railway.env (git-ignored; it holds your real keys, never share it or paste it in chat).
Leaves out the settings that only make sense on your laptop (APP_ENV, SQLITE_PATH, CLIENTS_DIR); the
Dockerfile sets the right ones for Railway. Prints only variable names and whether each is filled.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAPTOP_ONLY = {"APP_ENV", "SQLITE_PATH", "CLIENTS_DIR", "STORAGE_BACKEND", "LOG_LEVEL"}
NEEDED = ["TRUECHOICE_WHATSAPP_PHONE_NUMBER_ID", "TRUECHOICE_WHATSAPP_TOKEN", "TRUECHOICE_WHATSAPP_APP_SECRET",
          "TRUECHOICE_WHATSAPP_VERIFY_TOKEN", "TRUECHOICE_STAFF_NUMBERS", "SARVAM_API_KEY", "GROQ_API_KEY",
          "LLM_CHAIN"]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    source = ROOT / ".env"
    if not source.exists():
        print("No .env found.")
        return 1
    kept: list[str] = []
    values: dict[str, str] = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, value = text.split("=", 1)
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name in LAPTOP_ONLY or not value:
            continue
        values[name] = value
        kept.append(f"{name}={value}")
    out = ROOT / "data" / "railway.env"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(kept) + "\n", encoding="utf-8")
    print(f"Wrote {out} with {len(kept)} variables:")
    for name in sorted(values):
        print(f"  {name}  (filled, {len(values[name])} characters)")
    missing = [n for n in NEEDED if n not in values]
    if missing:
        print("\nMissing or empty (the bot needs these): " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
