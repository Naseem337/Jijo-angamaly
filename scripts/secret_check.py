"""Block secrets and data files from git.

    python scripts/secret_check.py --staged   # pre-commit hook: checks staged files
    python scripts/secret_check.py            # checks every tracked file

Prints only file:line and the kind of secret, never the value. Exit 1 if anything is found.
Install the hook once with:  python scripts/install_hooks.py
"""
from __future__ import annotations

import re
import subprocess
import sys

# Built from pieces so this file never matches itself.
_P = {
    "Anthropic key": "sk" + r"-ant-[A-Za-z0-9_\-]{8,}",
    "Razorpay live key": "rzp" + r"_live_[A-Za-z0-9]{8,}",
    "Razorpay test key": "rzp" + r"_test_[A-Za-z0-9]{8,}",
    "Meta / WhatsApp token": r"\bEA" + r"A[A-Za-z0-9]{20,}",
    "Groq key": "gs" + r"k_[A-Za-z0-9]{16,}",
}
PATTERNS = {name: re.compile(p) for name, p in _P.items()}

# Files that must never be committed (keys or guest data). .env.example is the allowed template.
_BLOCKED_FILE = re.compile(r"(^|/)(\.env(\..+)?|[^/]*\.env)$|\.(sqlite3?|db)$", re.I)
_ALLOWED_FILES = {".env.example"}
_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".ogg", ".mp3", ".wav")


def blocked_file(path: str) -> bool:
    path = path.replace("\\", "/")
    return path not in _ALLOWED_FILES and bool(_BLOCKED_FILE.search(path))


def find_secrets(text: str) -> list[tuple[int, str]]:
    """(line number, secret kind) for every hit. Never returns the value."""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for name, pat in PATTERNS.items():
            if pat.search(line):
                hits.append((i, name))
    return hits


def check(files: dict[str, str]) -> list[str]:
    """files = {path: text}. Returns problem lines (no secret values)."""
    problems = []
    for path, text in files.items():
        if blocked_file(path):
            problems.append(f"{path}: this file type must never be committed (.env / database)")
            continue
        if path.lower().endswith(_SKIP_EXT):
            continue
        for line, kind in find_secrets(text):
            problems.append(f"{path}:{line}: looks like a {kind}")
    return problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="ignore", check=True).stdout


def _read(path: str, staged: bool) -> str:
    if staged:
        return _git("show", f":{path}")
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def collect(staged: bool) -> dict[str, str]:
    if staged:
        names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
    else:
        names = _git("ls-files").splitlines()
    out = {}
    for n in filter(None, names):
        try:
            out[n] = "" if n.lower().endswith(_SKIP_EXT) else _read(n, staged)
        except (OSError, subprocess.CalledProcessError):
            out[n] = ""
    return out


def main(argv: list[str]) -> int:
    staged = "--staged" in argv
    problems = check(collect(staged))
    if problems:
        print("SECRET CHECK FAILED. Remove these before committing:")
        for p in problems:
            print("  " + p)
        print("Secrets belong only in .env (local) and Railway variables.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
