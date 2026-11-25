"""Token use of the resort agent, from agent_tokens.jsonl (written by app/resort/agent/usage.py, one line per turn).

    .venv\\Scripts\\python.exe scripts\\token_report.py                       next to RESORT_DB_PATH (or data/)
    .venv\\Scripts\\python.exe scripts\\token_report.py --file path\\to\\agent_tokens.jsonl
    .venv\\Scripts\\python.exe scripts\\token_report.py --days 7

Prints the average tokens per turn, per booking chat (a chat in which create_booking ran) and the cache hit rate
(cache-read tokens / all input tokens). The file has no guest text and no phone numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE_NAME = "agent_tokens.jsonl"


def default_file() -> Path:
    if env := os.environ.get("AGENT_TOKEN_LOG", "").strip():
        return Path(env)
    db = os.environ.get("RESORT_DB_PATH", "").strip()
    return (Path(db).parent if db else ROOT / "data") / FILE_NAME


def load(path: Path, days: int | None = None) -> list[dict]:
    rows = []
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if since is not None:
            try:
                if datetime.fromisoformat(row["at"]) < since:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
        rows.append(row)
    return rows


def report(rows: list[dict]) -> list[str]:
    if not rows:
        return ["No agent turns logged yet."]
    n = len(rows)
    tin = sum(r.get("input", 0) for r in rows)
    tout = sum(r.get("output", 0) for r in rows)
    read = sum(r.get("cache_read", 0) for r in rows)
    write = sum(r.get("cache_write", 0) for r in rows)
    chats: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        chats[r.get("chat", "?")].append(r)
    booking = [c for c in chats.values() if any(r.get("booking") for r in c)]
    per_booking = sum(r.get("input", 0) + r.get("output", 0) for c in booking for r in c) // len(booking) if booking else 0
    models = sorted({r.get("model") or "-" for r in rows})
    return [
        f"Turns: {n}   chats: {len(chats)}   booking chats: {len(booking)}   models: {', '.join(models)}",
        f"Average per turn: {(tin + tout) // n:,} tokens (input {tin // n:,}, output {tout // n:,}, "
        f"AI calls {sum(r.get('llm_calls', 0) for r in rows) / n:.1f}, tool rounds {sum(r.get('rounds', 0) for r in rows) / n:.1f})",
        f"Average per booking chat: {per_booking:,} tokens" if booking else "Average per booking chat: - (no booking yet)",
        f"Cache: hit rate {100 * read / tin:.0f}% (read {read:,} of {tin:,} input tokens), written {write:,}"
        if tin else "Cache: - (no input tokens)",
    ]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=None)
    ap.add_argument("--days", type=int, default=None, help="only the last N days")
    a = ap.parse_args()
    path = a.file or default_file()
    if not path.exists():
        print(f"No token log at {path} yet (it appears after the first AI turn).")
        return 0
    print("\n".join(report(load(path, a.days))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
