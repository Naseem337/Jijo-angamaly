from datetime import datetime, timezone

import pytest

from app.core.clock import IST, FixedClock, in_window, ist_today, to_ist


def test_fixed_clock_is_utc_and_advances(clock: FixedClock) -> None:
    assert clock.now().tzinfo == timezone.utc
    assert to_ist(clock.now()).hour == 10
    clock.advance(hours=25)
    assert to_ist(clock.now()).day == 17


def test_fixed_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 1, 1))


def test_ist_date_rolls_over_at_ist_midnight() -> None:
    # 18:45 UTC = 00:15 IST next day.
    c = FixedClock(datetime(2026, 10, 16, 18, 45, tzinfo=timezone.utc))
    assert ist_today(c).isoformat() == "2026-10-17"


@pytest.mark.parametrize("hh, mm, expected", [
    (8, 59, False), (9, 0, True), (17, 0, True), (19, 59, True), (20, 0, False),
])
def test_send_window(hh: int, mm: int, expected: bool) -> None:
    when = datetime(2026, 10, 16, hh, mm, tzinfo=IST)
    assert in_window(when, "09:00", "20:00") is expected


def test_cron_time_is_5pm_ist() -> None:
    # EventBridge cron(30 11 * * ? *) fires 11:30 UTC.
    fired = datetime(2026, 10, 16, 11, 30, tzinfo=timezone.utc)
    assert to_ist(fired).strftime("%H:%M") == "17:00"
    assert in_window(fired, "09:00", "20:00")
