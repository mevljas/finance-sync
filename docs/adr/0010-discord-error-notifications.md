# ADR-0010: Discord Error Notifications Across Sync Workers

- **Status**: Accepted
- **Date**: 2026-11-18
- **Deciders**: User, Antigravity Agent

## Context

`tr_worker` already uses a Discord Bot (`DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` via Discord REST API v10) for interactive 2FA renewal (see ADR-0003). Meanwhile, `ibkr_worker` and `tr_worker` run headlessly in Portainer containers (`run_forever()`), where unhandled sync exceptions and `ERROR`-level logs are only written to container stdout/stderr (`shared/scheduler.py` logs `logger.exception(...)` and sleeps until the next cycle).

Operators want immediate visibility in Discord whenever errors occur in either worker without requiring a separate alerting integration or failing when Discord credentials are absent.

## Decision

1. **Shared Non-Mandatory Discord Configuration (`config/settings.py` + `docker-compose.yml`)**:
   - Both `ibkr_worker` and `tr_worker` reuse the existing `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` channel configuration.
   - Error alerting is **optional**: `Settings.has_discord` returns `True` only when both `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` are configured with non-empty, non-placeholder values. When unconfigured, error alerting is silently disabled.
   - `docker-compose.yml` passes `${DISCORD_BOT_TOKEN:-}` and `${DISCORD_CHANNEL_ID:-}` optionally to `ibkr_worker` while keeping them configured on `tr_worker`.
2. **Trigger Scope (`shared/discord.py` + `logging.Handler`)**:
   - Extract a shared Discord REST v10 notifier in `shared/discord.py` (used by both error alerting and `tr_worker`'s `Discord2FAClient`).
   - Attach an `ERROR`-level `logging.Handler` (`DiscordErrorHandler`) during worker execution so **both** unhandled sync-iteration exceptions (logged via `logger.exception(...)` in `shared/scheduler.py` or fatal `--once` CLI failures) **and** any `ERROR`/`CRITICAL` log records emitted during a sync trigger a Discord notification.
   - The handler uses a re-entrancy guard and ignores errors originating from the Discord notifier itself so a Discord API outage cannot cause infinite recursion or crash a sync worker.
3. **No Alert Deduplication or Cooldown**:
   - Every `ERROR`-level event and failed sync iteration dispatches a Discord notification immediately so persistent outages remain visible on each cycle.
4. **Dry-Run Suppression, Secret Redaction & Payload Limit**:
   - Per ADR-0005, live Discord HTTP `POST` alerts are suppressed when `DRY_RUN=true` or `USE_MOCK_DATA=true` (logging `[DRY_RUN] Would send Discord error alert` instead).
   - Known `SecretStr` values (`BUDGETBAKERS_TOKEN`, `BB_COUCHDB_PASSWORD`, `IBKR_FLEX_TOKEN`, `TR_PIN`, `DISCORD_BOT_TOKEN`) are masked before formatting, and messages (worker name, error summary, and truncated traceback) are capped within Discord's 2000-character message limit.

## Consequences

- **Positive**: Any error in either `ibkr_worker` or `tr_worker` surfaces immediately in the existing homelab Discord channel with zero mandatory extra configuration.
- **Negative / Trade-offs**: Without cooldown/deduplication, a sustained outage emits one Discord message per failed polling cycle (every 15 minutes for `tr_worker`, every 6 hours for `ibkr_worker`).
