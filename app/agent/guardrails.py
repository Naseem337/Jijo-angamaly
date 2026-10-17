"""Post-checks on every reply before it is sent.

1. Unsourced figures: every rupee amount, km figure and model year in the reply must
   appear in a tool result from this turn, an earlier bot message, or the customer's own
   words. Figures are read in English, Manglish and Malayalam ("8L", "ettu lakh",
   "അഞ്ചര ലക്ഷം") by app.core.money.
2. Promises: discounts, guaranteed approvals, "best price" and the like.

The agent asks the model to rewrite a failing reply once; if that fails too, it sends a
safe "let me check with our team" message and hands the chat to staff.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.core.money import extract_numbers, normalize_text

YEAR_MIN, YEAR_MAX = 1990, 2100
_TOLERANCE = 0.01  # quoting ₹5.45 lakh as "5.5 lakh" or 42,000 km as "42k" is fine

_PROMISE_PATTERNS = [
    r"\bguarantee[sd]?\b", r"\bguaranteed\b", r"\b100\s*%",
    r"\b(?:loan|finance|emi)\b[^.!?\n]{0,40}\bapproved\b", r"\bapproved\b[^.!?\n]{0,40}\b(?:loan|finance)\b",
    r"\bpre-?approved\b", r"\b(?:best|lowest|final|special)\s+price\b",
    r"\b(?:can|will|could)\s+(?:give|offer|get)\s+(?:you\s+)?(?:a\s+)?(?:\w+\s+)?discount",
    r"\bdiscount\s+of\b", r"\b\d[\d,.]*\s*(?:k|l|lakh|lakhs|rs|₹)?\s*(?:off|discount)\b",
    r"\bfree\s+(?:service|insurance|accessories|warranty|transfer)\b",
    r"ഉറപ്പ്", r"ഉറപ്പായും", r"ഡിസ്കൗണ്ട്", r"\bdiscount\s+(?:tharam|tharaam|kittum|undakum)\b",
    r"\burappa(?:yum)?\b",
]
_PROMISE_RE = re.compile("|".join(_PROMISE_PATTERNS), re.IGNORECASE)


@dataclass
class SourcedFacts:
    """Every number the bot is allowed to repeat in this turn."""

    numbers: set[float] = field(default_factory=set)

    def add_text(self, text: str) -> None:
        for parsed in extract_numbers(text):
            self.numbers.add(parsed.value)
        for digits in re.findall(r"\d+(?:\.\d+)?", normalize_text(text)):
            self.numbers.add(float(digits))

    def add_json(self, text: str) -> None:
        """Tool results: every number anywhere in the JSON, plus numbers inside strings."""
        try:
            data = json.loads(text)
        except ValueError:
            self.add_text(text)
            return
        self._walk(data)

    def _walk(self, value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            self.numbers.add(float(value))
        elif isinstance(value, str):
            self.add_text(value)
        elif isinstance(value, dict):
            for item in value.values():
                self._walk(item)
        elif isinstance(value, list):
            for item in value:
                self._walk(item)

    def allows(self, value: float, *, exact: bool = False) -> bool:
        if exact:
            return value in self.numbers
        return any(abs(value - known) <= max(_TOLERANCE * known, 0.5) for known in self.numbers)


@dataclass(frozen=True)
class Violation:
    kind: str  # "unsourced_amount" | "unsourced_km" | "unsourced_year" | "promise"
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def check_reply(text: str, facts: SourcedFacts) -> list[Violation]:
    problems: list[Violation] = []
    for parsed in extract_numbers(text):
        value = parsed.value
        if parsed.unit == "inr" and value >= 1000 and not facts.allows(value):
            problems.append(Violation("unsourced_amount", parsed.raw))
        elif parsed.unit == "km" and value >= 100 and not facts.allows(value):
            problems.append(Violation("unsourced_km", parsed.raw))
        elif (parsed.unit == "none" and value.is_integer() and YEAR_MIN <= value <= YEAR_MAX
              and not facts.allows(value, exact=True)):
            problems.append(Violation("unsourced_year", parsed.raw))
    for match in _PROMISE_RE.finditer(text):
        problems.append(Violation("promise", match.group(0)))
    return problems


def facts_from(texts: Iterable[str] = (), tool_results: Iterable[str] = (),
               years: Iterable[int] = ()) -> SourcedFacts:
    facts = SourcedFacts()
    for text in texts:
        facts.add_text(text)
    for result in tool_results:
        facts.add_json(result)
    facts.numbers.update(float(y) for y in years)
    return facts
