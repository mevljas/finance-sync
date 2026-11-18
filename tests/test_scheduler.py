"""Unit tests for Settings scheduling fields and shared.scheduler."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfoNotFoundError

import pytest
from config.settings import Settings
from pydantic import ValidationError
from shared.scheduler import (
    compute_next_scheduled_run,
    parse_start_time,
    resolve_timezone,
    run_periodic_sync,
)


class FakeClock:
    """Deterministic controllable clock and sleep recorder for scheduler tests."""

    def __init__(self, initial_utc: datetime) -> None:
        self.now_utc = initial_utc.astimezone(UTC)
        self.sleeps: list[float] = []
        self.run_timestamps_utc: list[datetime] = []

    def now(self) -> datetime:
        """Return current simulated UTC datetime."""
        return self.now_utc

    def sleep(self, seconds: float) -> None:
        """Advance simulated clock without real wall-clock delay."""
        self.sleeps.append(seconds)
        self.now_utc += timedelta(seconds=seconds)


def test_settings_sync_start_time_and_timezone_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Settings parses SYNC_START_TIME and SYNC_TIMEZONE and normalizes blanks."""
    monkeypatch.delenv("SYNC_START_TIME", raising=False)
    monkeypatch.delenv("SYNC_TIMEZONE", raising=False)
    default_settings = Settings()
    assert default_settings.sync_start_time is None
    assert default_settings.sync_timezone is None

    monkeypatch.setenv("SYNC_START_TIME", "   ")
    monkeypatch.setenv("SYNC_TIMEZONE", "")
    blank_settings = Settings()
    assert blank_settings.sync_start_time is None
    assert blank_settings.sync_timezone is None

    monkeypatch.setenv("SYNC_START_TIME", "08:05")
    monkeypatch.setenv("SYNC_TIMEZONE", "UTC")
    hhmm_settings = Settings()
    assert hhmm_settings.sync_start_time == time(8, 5, 0)
    assert hhmm_settings.sync_timezone == "UTC"

    monkeypatch.setenv("SYNC_START_TIME", "23:59:30")
    monkeypatch.delenv("SYNC_TIMEZONE", raising=False)
    hhmmss_settings = Settings()
    assert hhmmss_settings.sync_start_time == time(23, 59, 30)

    monkeypatch.setenv("SYNC_START_TIME", "25:00")
    with pytest.raises(ValidationError):
        Settings()

    monkeypatch.setenv("SYNC_START_TIME", "08:00+01:00")
    with pytest.raises(ValidationError):
        Settings()

    monkeypatch.delenv("SYNC_START_TIME", raising=False)
    monkeypatch.setenv("SYNC_TIMEZONE", "Invalid/Nowhere_Zone")
    with pytest.raises(ValidationError):
        Settings()


def test_parse_start_time_variants() -> None:
    """Verify parse_start_time accepts HH:MM, HH:MM:SS, time objects, and blanks."""
    assert parse_start_time(None) is None
    assert parse_start_time("") is None
    assert parse_start_time("  ") is None
    assert parse_start_time(time(7, 30)) == time(7, 30)
    assert parse_start_time("07:30") == time(7, 30, 0)
    assert parse_start_time("07:30:45") == time(7, 30, 45)

    with pytest.raises(ValueError, match="HH:MM"):
        parse_start_time("not-a-time")


def test_resolve_timezone_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify resolve_timezone handles UTC, fixed offsets, and invalid IANA names."""
    assert resolve_timezone("UTC") is UTC
    assert resolve_timezone("z") is UTC
    assert resolve_timezone("+02:00") == timezone(timedelta(hours=2))
    assert resolve_timezone("-05:30") == timezone(timedelta(hours=-5, minutes=-30))

    monkeypatch.setenv("TZ", "UTC")
    assert resolve_timezone(None) is UTC

    with pytest.raises(ZoneInfoNotFoundError):
        resolve_timezone("Invalid/Nowhere_Zone")


def test_compute_next_scheduled_run_advances_past_overruns() -> None:
    """Verify compute_next_scheduled_run skips missed slots cleanly."""
    anchor = datetime(2026, 11, 18, 8, 0, 0, tzinfo=UTC)
    assert compute_next_scheduled_run(
        anchor_utc=anchor,
        interval_seconds=3600,
        now_utc=datetime(2026, 11, 18, 8, 0, 5, tzinfo=UTC),
    ) == datetime(2026, 11, 18, 9, 0, 0, tzinfo=UTC)

    # Sync took 2h10m (> 1h interval): next slot should jump to 11:00, not 09:00
    assert compute_next_scheduled_run(
        anchor_utc=anchor,
        interval_seconds=3600,
        now_utc=datetime(2026, 11, 18, 10, 10, 0, tzinfo=UTC),
    ) == datetime(2026, 11, 18, 11, 0, 0, tzinfo=UTC)


def test_scheduler_without_start_time_runs_immediately_at_startup() -> None:
    """Verify unset start_time runs immediately and sleeps interval_seconds each cycle."""
    clock = FakeClock(datetime(2026, 11, 18, 19, 0, 0, tzinfo=UTC))

    def sync() -> None:
        clock.run_timestamps_utc.append(clock.now_utc)
        clock.now_utc += timedelta(seconds=10)

    run_periodic_sync(
        worker_name="test",
        interval_seconds=900,
        sync_fn=sync,
        start_time=None,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        max_iterations=3,
    )

    assert clock.run_timestamps_utc == [
        datetime(2026, 11, 18, 19, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 18, 19, 15, 10, tzinfo=UTC),
        datetime(2026, 11, 18, 19, 30, 20, tzinfo=UTC),
    ]
    assert clock.sleeps == [900.0, 900.0]


def test_scheduler_waits_when_start_time_is_in_future_today() -> None:
    """Verify startup before today's start_time waits until start_time then anchors."""
    # 06:00 UTC with start_time=08:00 UTC and 6h (21600s) interval
    clock = FakeClock(datetime(2026, 11, 18, 6, 0, 0, tzinfo=UTC))

    def sync() -> None:
        clock.run_timestamps_utc.append(clock.now_utc)
        # Simulate sync taking 30s; next slot must still be anchored to 14:00, 20:00
        clock.now_utc += timedelta(seconds=30)

    run_periodic_sync(
        worker_name="ibkr",
        interval_seconds=21600,
        sync_fn=sync,
        start_time=time(8, 0, 0),
        timezone_name="UTC",
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        max_iterations=3,
    )

    assert clock.run_timestamps_utc == [
        datetime(2026, 11, 18, 8, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 18, 14, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 18, 20, 0, 0, tzinfo=UTC),
    ]
    assert clock.sleeps == [7200.0, 21570.0, 21570.0]


def test_scheduler_runs_immediately_then_tomorrow_start_when_start_time_passed() -> None:
    """Verify startup after today's start_time runs right away, then tomorrow at start_time."""
    # Local timezone +01:00, startup at 19:30+01:00 (18:30 UTC), start_time=08:00+01:00
    clock = FakeClock(datetime(2026, 11, 18, 18, 30, 0, tzinfo=UTC))

    def sync() -> None:
        clock.run_timestamps_utc.append(clock.now_utc)
        clock.now_utc += timedelta(seconds=20)

    run_periodic_sync(
        worker_name="ibkr",
        interval_seconds=21600,
        sync_fn=sync,
        start_time=time(8, 0, 0),
        timezone_name="+01:00",
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        max_iterations=4,
    )

    assert clock.run_timestamps_utc == [
        # 1. Immediate run at 19:30+01:00 (18:30 UTC)
        datetime(2026, 11, 18, 18, 30, 0, tzinfo=UTC),
        # 2. Tomorrow 08:00+01:00 (07:00 UTC)
        datetime(2026, 11, 19, 7, 0, 0, tzinfo=UTC),
        # 3. Tomorrow 14:00+01:00 (13:00 UTC)
        datetime(2026, 11, 19, 13, 0, 0, tzinfo=UTC),
        # 4. Tomorrow 20:00+01:00 (19:00 UTC)
        datetime(2026, 11, 19, 19, 0, 0, tzinfo=UTC),
    ]
    # First sleep: from 18:30:20 UTC to 07:00:00 UTC = 12h 29m 40s = 44980s
    assert clock.sleeps == [44980.0, 21580.0, 21580.0]


def test_scheduler_continues_on_sync_exception() -> None:
    """Verify an exception in sync_fn is logged and does not break the schedule."""
    clock = FakeClock(datetime(2026, 11, 18, 7, 59, 0, tzinfo=UTC))
    attempts = 0

    def flaky_sync() -> None:
        nonlocal attempts
        attempts += 1
        clock.run_timestamps_utc.append(clock.now_utc)
        if attempts == 1:
            raise RuntimeError("transient error")

    run_periodic_sync(
        worker_name="tr",
        interval_seconds=900,
        sync_fn=flaky_sync,
        start_time=time(8, 0, 0),
        timezone_name="UTC",
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        max_iterations=2,
    )

    assert attempts == 2
    assert clock.run_timestamps_utc == [
        datetime(2026, 11, 18, 8, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 18, 8, 15, 0, tzinfo=UTC),
    ]
