"""The outbox (docs/AI_AGENT_DESIGN.md sections 1 and 4): messages are queued in order, then sent together.

Everything that leaves the agent goes through here: texts and photos for the guest, and alerts for the staff. A DRY-RUN
outbox only records what would have gone out and never calls WhatsApp (the shadow mode of Step AI-6 uses it); only a live
outbox sends, and only the live senders (bot_outbox) log each message with the WhatsApp id WhatsApp returns, so a
swipe-reply to a photo or a room label can find its room. Never-same-text-twice, the AI's own line first and the
never-silent check come in Step AI-4.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Item:
    kind: str  # "text" | "photos" | "staff" | "marker"
    key: str  # what it is, e.g. "rooms_ok", "ask_contact", "ai" (the AI's own line), "photos:6", "staff:handoff"
    text: str = ""
    refs: tuple[str, ...] = ()  # photo references
    to: tuple[str, ...] = ()  # staff alerts: the numbers they go to


class Outbox:
    def __init__(self, phone: str, send_text: Callable[[str, str], object] | None = None,
                 send_photos: Callable[[str, list[str]], object] | None = None, *, dry_run: bool = False,
                 send_staff: Callable[[str, str], object] | None = None) -> None:
        self.phone = phone
        self.dry_run = dry_run
        self._send_text, self._send_photos, self._send_staff = send_text, send_photos, send_staff
        self.queue: list[Item] = []
        self.sent: list[Item] = []  # what went out (dry run: what WOULD have gone out), in order

    def text(self, key: str, text: str) -> None:
        self.queue.append(Item("text", key, text=text))

    def photos(self, key: str, refs: list[str]) -> None:
        self.queue.append(Item("photos", key, refs=tuple(refs)))

    def staff(self, key: str, text: str, numbers: list[str]) -> None:
        """An alert for the resort's team (never the guest): a dry run only records it."""
        self.queue.append(Item("staff", key, text=text, to=tuple(numbers)))

    def marker(self, key: str) -> None:
        """A placeholder in `sent` that sends nothing."""
        self.queue.append(Item("marker", key))

    @property
    def keys(self) -> list[str]:
        """The keys sent or queued this turn: the tool result's `sent` list."""
        return [i.key for i in (*self.sent, *self.queue)]

    def flush(self) -> list[Item]:
        """Send the queue in order. A dry-run outbox only records. Returns what was sent (or would have been)."""
        done: list[Item] = []
        for item in self.queue:
            if not self.dry_run:
                if item.kind == "text" and self._send_text is not None:
                    self._send_text(self.phone, item.text)
                elif item.kind == "photos" and self._send_photos is not None:
                    self._send_photos(self.phone, list(item.refs))
                elif item.kind == "staff" and self._send_staff is not None:
                    for number in item.to:
                        try:
                            self._send_staff(number, item.text)
                        except Exception as exc:  # outside their 24 h window or a network error: logged, never fatal
                            log.warning("Staff alert to %s failed: %s", number[-4:], exc)
            done.append(item)
        self.queue = []
        self.sent.extend(done)
        return done


def bot_outbox(bot: Any, phone: str, *, dry_run: bool = False) -> Outbox:
    """An outbox that sends through the BookingBot: texts and photos go to WhatsApp and are logged in the chat with the
    WhatsApp id WhatsApp returns, so a swipe-reply to a photo or a room label finds its room (bot._note_quote)."""
    return Outbox(phone, lambda to, text: bot._text(to, text, repeat_ok=True), lambda to, refs: bot._send_photos(to, refs),
                  dry_run=dry_run, send_staff=lambda to, text: bot.wa.send_text(to, text))
