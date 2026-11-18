"""Periodic scheduling helpers supporting optional daily start-time anchoring."""

from __future__ import annotations

import logging
import os
import re
import time as time_module
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_OFFSET_PATTERN = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def parse_start_time(value: str | time | None) -> time | None:
    """Parse an optional HH:MM or HH:MM:SS wall-clock start time."""
    if value is None:
        return None
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise ValueError(
                "Start time must not include a timezone offset; use SYNC_TIMEZONE or TZ."
            )
        return value

    stripped = value.strip()
    if not stripped:
        return None

    match = _TIME_PATTERN.fullmatch(stripped)
    if match is None:
        raise ValueError(f"Invalid start time {value!r}; expected HH:MM or HH:MM:SS.")

    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    return time(hour=hour, minute=minute, second=second)


def resolve_timezone(tz_name: str | None = None) -> tzinfo:
    """Resolve explicit SYNC_TIMEZONE, TZ environment variable, or local system timezone."""
    candidate = (tz_name or "").strip() or os.environ.get("TZ", "").strip()
    if not candidate:
        return datetime.now().astimezone().tzinfo or UTC

    if candidate.upper() in {"UTC", "Z"}:
        return UTC

    offset_match = _OFFSET_PATTERN.fullmatch(candidate)
    if offset_match is not None:
        sign = 1 if offset_match.group(1) == "+" else -1
        hours = int(offset_match.group(2))
        minutes = int(offset_match.group(3))
        if hours >= 24 or minutes >= 60:
            raise ValueError(f"Invalid UTC offset timezone: {candidate!r}")
        return timezone(sign * timedelta(hours=hours, minutes=minutes))

    return ZoneInfo(candidate)


def compute_next_scheduled_run(
    anchor_utc: datetime,
    interval_seconds: int,
    now_utc: datetime,
) -> datetime:
    """Return the earliest slot `anchor_utc + k * interval_seconds` strictly after `now_utc`."""
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be positive, got {interval_seconds}")

    step = timedelta(seconds=interval_seconds)
    if anchor_utc > now_utc:
        return anchor_utc

    elapsed = (now_utc - anchor_utc).total_seconds()
    steps = int(elapsed // interval_seconds) + 1
    candidate = anchor_utc + steps * step
    while candidate <= now_utc:
        candidate += step
    return candidate


def _run_safely(worker_name: str, sync_fn: Callable[[], object]) -> None:
    """Execute one sync iteration, logging exceptions so the worker loop survives."""
    try:
        sync_fn()
    except Exception:
        logger.exception("Error during %s sync iteration", worker_name)


def run_periodic_sync(
    *,
    worker_name: str,
    interval_seconds: int,
    sync_fn: Callable[[], object],
    start_time: time | None = None,
    timezone_name: str | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] = time_module.sleep,
    max_iterations: int | None = None,
) -> None:
    """Run `sync_fn` periodically, optionally anchored to a daily `start_time`."""
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be positive, got {interval_seconds}")
    if max_iterations is not None and max_iterations <= 0:
        return

    clock = now_fn or (lambda: datetime.now(UTC))

    if start_time is None:
        logger.info(
            "Starting %s worker loop (interval=%ds, immediate startup)",
            worker_name,
            interval_seconds,
        )
        iterations = 0
        while True:
            _run_safely(worker_name, sync_fn)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return
            sleep_fn(float(interval_seconds))

    tz = resolve_timezone(timezone_name)
    startup_utc = clock().astimezone(UTC)
    local_now = startup_utc.astimezone(tz)
    today_start_local = datetime.combine(local_now.date(), start_time, tzinfo=tz)
    today_start_utc = today_start_local.astimezone(UTC)

    logger.info(
        "Starting %s worker loop (interval=%ds, start_time=%s, tz=%s)",
        worker_name,
        interval_seconds,
        start_time.isoformat(),
        tz,
    )

    if startup_utc < today_start_utc:
        anchor_utc = today_start_utc
        initial_delay = (today_start_utc - startup_utc).total_seconds()
        logger.info(
            "Waiting %.1fs until first %s sync at %s",
            initial_delay,
            worker_name,
            today_start_local.isoformat(),
        )
        if initial_delay > 0:
            sleep_fn(initial_delay)
        _run_safely(worker_name, sync_fn)
        iterations = 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        next_run_utc = compute_next_scheduled_run(
            anchor_utc + timedelta(seconds=interval_seconds),
            interval_seconds,
            clock().astimezone(UTC),
        )
    else:
        logger.info(
            "Configured start_time (%s) already passed today; running initial %s sync immediately",
            start_time.isoformat(),
            worker_name,
        )
        tomorrow_local = datetime.combine(
            local_now.date() + timedelta(days=1),
            start_time,
            tzinfo=tz,
        )
        anchor_utc = tomorrow_local.astimezone(UTC)
        _run_safely(worker_name, sync_fn)
        iterations = 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        next_run_utc = compute_next_scheduled_run(
            anchor_utc,
            interval_seconds,
            clock().astimezone(UTC),
        )

    while True:
        now_utc = clock().astimezone(UTC)
        delay = (next_run_utc - now_utc).total_seconds()
        if delay > 0:
            sleep_fn(delay)
        _run_safely(worker_name, sync_fn)
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        next_run_utc = compute_next_scheduled_run(
            anchor_utc,
            interval_seconds,
            clock().astimezone(UTC),
        )
