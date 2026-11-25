"""Token use per agent turn: one log line and one JSON line in agent_tokens.jsonl (next to the resort database).

Never stored or logged: guest text, phone numbers, API keys. A chat is a short one-way hash of the phone, only so
scripts/token_report.py can add up the turns of one booking chat.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILE_NAME = "agent_tokens.jsonl"


def chat_id(phone: str) -> str:
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()[:12]


def log_path(repo: Any) -> Path | None:
    """AGENT_TOKEN_LOG if set, else agent_tokens.jsonl next to the resort database file (None for an in-memory one)."""
    if env := os.environ.get("AGENT_TOKEN_LOG", "").strip():
        return Path(env)
    try:
        row = repo._db.execute("PRAGMA database_list").fetchone()
        db_file = row["file"] if row is not None else ""
    except Exception:
        return None
    return Path(db_file).parent / FILE_NAME if db_file else None


def row(result: Any, phone: str, at: str) -> dict[str, Any]:
    names = [c["name"] for c in result.calls]
    return {"at": at, "chat": chat_id(phone), "model": result.model, "llm_calls": result.llm_calls,
            "input": result.input_tokens, "cache_read": result.cache_read_tokens,
            "cache_write": result.cache_write_tokens, "output": result.output_tokens, "rounds": result.rounds,
            "tools": names, "booking": "create_booking" in names, "outcome": result.outcome}


def record(bot: Any, phone: str, result: Any, *, dry_run: bool = False) -> None:
    """Best effort: a failure here never touches the guest's turn."""
    if not result.llm_calls:
        return
    log.info("Agent tokens: model=%s calls=%d input=%d cache_read=%d cache_write=%d output=%d rounds=%d",
             result.model or "-", result.llm_calls, result.input_tokens, result.cache_read_tokens,
             result.cache_write_tokens, result.output_tokens, result.rounds)
    if dry_run:
        return
    try:
        path = log_path(bot.repo)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row(result, phone, bot.clock.now().isoformat())) + "\n")
    except Exception as exc:
        log.warning("Token log not written: %s", type(exc).__name__)
