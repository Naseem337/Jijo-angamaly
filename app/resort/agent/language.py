"""The reply language of one turn (live bug 2026-11-25: an English guest got the owner's texts in Manglish).

The AI judges the language of the guest's LATEST message and passes it as `language` on every tool call. Code never
reads the guest's words for it. The language of each turn is stored per conversation in the guest's state (key
`agent_lang`):

    {"conv": <when this conversation started>, "style": <the last turn's language>, "lock": <a language the guest asked
     for ('English please'), or null>}

- The AI's value is missing or invalid: the last turn's language, ONLY when that turn was in this same conversation;
  otherwise "en". A language never carries across a restart (the state is cleared) or a new conversation (12 h gap,
  confirmed booking).
- A lock (set_language) holds for the rest of the conversation: every tool and every owner text use it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.resort.agent.state import LANGUAGES

STATE_KEY = "agent_lang"
DEFAULT = "en"


def valid(value: Any) -> str:
    """The language style in `value` (any case), or '' when it is not one of LANGUAGES."""
    said = str(value or "").strip().lower()
    return said if said in LANGUAGES else ""


@dataclass
class TurnLanguage:
    conv: str  # when the current conversation started (conversation.remember)
    base: str = DEFAULT  # the fallback for this turn: the last turn's language in this conversation, else "en"
    lock: str | None = None  # the language the guest asked for in this conversation
    said: str = ""  # the latest valid language the AI passed this turn

    @property
    def now(self) -> str:
        """The language of this turn: the lock, else what the AI passed this turn, else the fallback."""
        return self.lock or self.said or self.base

    def take(self, value: Any) -> str:
        """One tool call's `language`: remembered when valid; returns the language that call must use."""
        if said := valid(value):
            self.said = said
        return self.now

    def set_lock(self, value: Any) -> str:
        if said := valid(value):
            self.lock = said
        return self.now


def load(bot: Any, phone: str, conv: str) -> TurnLanguage:
    """This turn's language state. A stored language from another conversation is ignored."""
    saved = bot.repo.get_state(phone).get(STATE_KEY) or {}
    if not isinstance(saved, dict) or saved.get("conv") != conv:
        return TurnLanguage(conv)
    return TurnLanguage(conv, base=valid(saved.get("style")) or DEFAULT, lock=valid(saved.get("lock")) or None)


def save(bot: Any, phone: str, lang: TurnLanguage) -> None:
    """Store this turn's language for the next turn of the same conversation."""
    state = bot.repo.get_state(phone)
    value = {"conv": lang.conv, "style": lang.now, "lock": lang.lock}
    if state.get(STATE_KEY) != value:
        state[STATE_KEY] = value
        bot.repo.set_state(phone, state)
