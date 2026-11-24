"""The outbox (docs/AI_AGENT_DESIGN.md sections 1 and 4): messages are queued in order, then sent together.

Skeleton for Step AI-1: ordered queue, a send callable, and a DRY-RUN mode that records what would have gone out
without sending anything (the shadow mode of Step AI-6 uses it). Never-same-text-twice, the AI's own line first and
the never-silent check come in Step AI-4.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    kind: str  # "text" | "photos"
    key: str  # what it is, e.g. "rooms_ok", "ask_contact", "ai" (the AI's own line), "photos:6"
    text: str = ""
    refs: tuple[str, ...] = ()  # photo references


class Outbox:
    def __init__(self, phone: str, send_text: Callable[[str, str], object] | None = None,
                 send_photos: Callable[[str, list[str]], object] | None = None, *, dry_run: bool = False) -> None:
        self.phone = phone
        self.dry_run = dry_run
        self._send_text, self._send_photos = send_text, send_photos
        self.queue: list[Item] = []
        self.sent: list[Item] = []  # what went out (dry run: what WOULD have gone out), in order

    def text(self, key: str, text: str) -> None:
        self.queue.append(Item("text", key, text=text))

    def photos(self, key: str, refs: list[str]) -> None:
        self.queue.append(Item("photos", key, refs=tuple(refs)))

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
            done.append(item)
        self.queue = []
        self.sent.extend(done)
        return done
