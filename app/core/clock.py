"""Injectable clock + IST helpers.

All business rules (send window, expiry, 24 h auto-resume…) take a Clock so tests can
freeze or advance time. India has no DST, so IST is a fixed +05:30 offset; this avoids
needing the tzdata package on Windows.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Test clock: starts at a given instant and only moves when told to."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when.astimezone(timezone.utc)

    def advance(self, **kwargs: float) -> None:
        """clock.advance(hours=25) etc."""
        self._now += timedelta(**kwargs)


def to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


def ist_today(clock: Clock) -> date:
    """Calendar date in India; daily caps and counters reset on this."""
    return to_ist(clock.now()).date()


def parse_hhmm(value: str) -> time:
    """'09:00' -> time(9, 0)."""
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def in_window(dt: datetime, start: str, end: str) -> bool:
    """True if dt (any tz) falls inside [start, end) in IST. Window must not cross midnight."""
    t = to_ist(dt).time()
    return parse_hhmm(start) <= t < parse_hhmm(end)
