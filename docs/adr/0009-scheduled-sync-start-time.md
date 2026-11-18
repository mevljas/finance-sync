# ADR-0009: Scheduled Sync Start Time (`SYNC_START_TIME`) and Anchored Polling

- **Status**: Accepted
- **Date**: 2026-11-18
- **Deciders**: User, Antigravity Agent

## Context

Previously, both `ibkr_worker` and `tr_worker` ran `sync_once()` unconditionally at container startup and then slept for a fixed `*_POLL_INTERVAL_SECONDS` (`21600s` for IBKR, `900s` for Trade Republic). Because `time.sleep(interval)` ran after each sync finished, sync execution times drifted forward over time and could not be aligned to a predictable daily clock schedule (for example, `08:00`, `14:00`, `20:00`, `02:00`).

Operators want a single shared option to anchor the first scheduled sync time across both workers while keeping immediate-at-startup behavior when unconfigured, and ensuring that restarting a container after today's configured start time still triggers an immediate sync without waiting up to 24 hours.

## Decision

1. **Shared Configuration (`SYNC_START_TIME` & `SYNC_TIMEZONE`)**:
   - `config/settings.py` defines `SYNC_START_TIME` (optional `HH:MM` or `HH:MM:SS` wall-clock time; empty/unset defaults to `None`) and `SYNC_TIMEZONE` (optional IANA timezone or UTC offset override), shared by both `ibkr_worker` and `tr_worker`.
   - Both workers also accept `--start-time HH:MM[:SS]` on the CLI for local runs, while one-shot CLI modes (`--once`, `--dry-run-output`, `--purge-account-records`, `--delete-file`, `--xml-file`) continue to execute immediately.
2. **Timezone Default & Override (`Dockerfile` + `docker-compose.yml`)**:
   - The Alpine runtime stage in `Dockerfile` installs `tzdata` and sets `ENV TZ="Europe/Ljubljana"` as the container default, overridable at runtime via either `TZ` or `SYNC_TIMEZONE`.
3. **Startup & Anchored Periodic Schedule (`shared/scheduler.py`)**:
   - **When `SYNC_START_TIME` is unset**: Run `sync_once()` immediately on startup and sleep `interval_seconds` between iterations (backward-compatible default).
   - **When `SYNC_START_TIME` is set**:
     - If startup occurs **before** today's `SYNC_START_TIME` (`startup_utc < today_start_utc`): sleep until `today_start_utc`, run `sync_once()`, and anchor subsequent runs to `today_start_utc + k * interval_seconds`.
     - If startup occurs **at or after** today's `SYNC_START_TIME` (`startup_utc >= today_start_utc`): run `sync_once()` **immediately** on startup, schedule the next run at **tomorrow's `SYNC_START_TIME`** (`tomorrow_start_utc`), and anchor subsequent runs to `tomorrow_start_utc + k * interval_seconds`.
   - Subsequent slots step in UTC elapsed seconds (`timedelta(seconds=interval_seconds)`) and skip any missed slots if a sync iteration overruns `interval_seconds`, preventing clock drift and DST double/skipped triggers.

## Consequences

- **Positive**: Daily sync times stay aligned across restarts and sync durations, restarting after the daily start time still syncs immediately, and existing deployments without `SYNC_START_TIME` behave identically to before.
- **Negative / Trade-offs**: Adds `tzdata` to the Alpine runtime image (~3 MB) to support IANA timezone lookups via `zoneinfo`.
