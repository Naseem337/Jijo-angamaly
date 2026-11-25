"""The outbox (docs/AI_AGENT_DESIGN.md sections 1 and 4): messages are queued in order, then sent together.

Everything that leaves the agent goes through here: texts and photos for the guest, and alerts for the staff. A DRY-RUN
outbox only records what would have gone out and never calls WhatsApp (the shadow mode of Step AI-6 uses it); only a live
outbox sends, and only the live senders (bot_outbox) log each message with the WhatsApp id WhatsApp returns, so a
swipe-reply to a photo or a room label can find its room.

Order in one turn (design section 4, put together by turn.py): the AI's own line (prepend_text) -> the tools' messages
in the order they were queued -> the open step only when nothing at all was queued (never silent). The AI's line is
dropped when the same text already went out in this conversation (guard.py e); tool messages are never dropped.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Item:
    kind: str  # "text" | "photos" | "staff" | "marker"
    key: str  # what it is, e.g. "rooms_ok", "ask_contact", "ai" (the AI's own line), "photos:6", "staff:handoff"
    text: str = ""
    refs: tuple[str, ...] = ()  # photo references
    to: tuple[str, ...] = ()  # staff alerts: the numbers they go to


EXACT_KEYS = ("guest_called",)  # owner texts sent word for word ('sir' is never taken out of them)
NEAR_SAME = 0.9  # two texts this alike (after case and punctuation) are the same message


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9₹]+", text.lower()))


def near_same(a: str, b: str) -> bool:
    """The same message: equal after case / punctuation / spaces, or (both 20+ letters) at least 90% alike."""
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    return x == y or (min(len(x), len(y)) >= 20 and SequenceMatcher(None, x, y).ratio() >= NEAR_SAME)


class Outbox:
    def __init__(self, phone: str, send_text: Callable[[str, str], object] | None = None,
                 send_photos: Callable[[str, list[str]], object] | None = None, *, dry_run: bool = False,
                 send_staff: Callable[[str, str], object] | None = None,
                 send_exact: Callable[[str, str], object] | None = None) -> None:
        self.phone = phone
        self.dry_run = dry_run
        self._send_text, self._send_photos, self._send_staff = send_text, send_photos, send_staff
        self._send_exact = send_exact
        self.queue: list[Item] = []
        self.sent: list[Item] = []  # what went out (dry run: what WOULD have gone out), in order

    def text(self, key: str, text: str) -> None:
        self.queue.append(Item("text", key, text=text))

    def prepend_text(self, key: str, text: str) -> None:
        """A text that goes BEFORE everything queued so far (the AI's own line comes first, design section 4)."""
        self.queue.insert(0, Item("text", key, text=text))

    def photos(self, key: str, refs: list[str]) -> None:
        self.queue.append(Item("photos", key, refs=tuple(refs)))

    def staff(self, key: str, text: str, numbers: list[str]) -> None:
        """An alert for the resort's team (never the guest): a dry run only records it."""
        self.queue.append(Item("staff", key, text=text, to=tuple(numbers)))

    def marker(self, key: str) -> None:
        """A placeholder in `sent` that sends nothing."""
        self.queue.append(Item("marker", key))

    @property
    def spoke(self) -> bool:
        """Something for the guest (a text or photos) was sent or queued this turn."""
        return any(i.kind in ("text", "photos") for i in (*self.sent, *self.queue))

    @property
    def texts(self) -> list[str]:
        """The texts for the guest sent or queued this turn (before the AI's line: the tools' messages)."""
        return [i.text for i in (*self.sent, *self.queue) if i.kind == "text"]

    @property
    def keys(self) -> list[str]:
        """The keys sent or queued this turn: the tool result's `sent` list."""
        return [i.key for i in (*self.sent, *self.queue)]

    def dedupe(self) -> list[Item]:
        """Never the same text twice in one turn (fix round 2): the AI's own line goes when it is (nearly) the same as a
        tool's message; of two (nearly) equal tool messages only the first stays. Returns what was dropped."""
        texts = [i for i in (*self.sent, *self.queue) if i.kind == "text"]
        queued = {id(i) for i in self.queue}
        gone: set[int] = set()
        for n, item in enumerate(texts):
            if id(item) not in queued:
                continue  # already sent this turn: never taken back
            if item.key == "ai":
                others = [o for o in texts if o is not item and o.key != "ai"]
            else:
                others = [o for o in texts[:n] if o.key != "ai" and id(o) not in gone]
            if any(near_same(item.text, o.text) for o in others):
                gone.add(id(item))
        dropped = [i for i in self.queue if id(i) in gone]
        self.queue = [i for i in self.queue if id(i) not in gone]
        if dropped:
            log.info("Outbox: %d repeated text(s) dropped for %s", len(dropped), self.phone[-4:])
        return dropped

    def flush(self) -> list[Item]:
        """Send the queue in order. A dry-run outbox only records. Returns what was sent (or would have been)."""
        done: list[Item] = []
        for item in self.queue:
            if not self.dry_run:
                if item.kind == "text" and item.key in EXACT_KEYS and self._send_exact is not None:
                    self._send_exact(self.phone, item.text)
                elif item.kind == "text" and self._send_text is not None:
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
                  dry_run=dry_run, send_staff=lambda to, text: bot.wa.send_text(to, text),
                  send_exact=lambda to, text: _send_exact(bot, to, text))


def _send_exact(bot: Any, phone: str, text: str) -> bool:
    """An owner text word for word: like bot._text (English letters only, no booking id, logged with its WhatsApp id) but
    without taking 'sir' out (bot._ease_sir), because the owner's exact wording has it."""
    from app.resort.chat import english_letters_only
    from app.resort.flow import BOOKING_ID

    text = BOOKING_ID.sub("", english_letters_only(text)).replace(" .", ".").strip()
    try:
        wamid = bot.wa.send_text(phone, text)
    except Exception as exc:  # WhatsApp error or network failure: logged, never crashes the turn
        log.error("Could not send to %s: %s", phone[-4:], exc)
        return False
    bot._log(phone, text, wamid=wamid if isinstance(wamid, str) else "")
    return True
