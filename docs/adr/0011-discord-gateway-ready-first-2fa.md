# ADR-0011: Ready-First Trade Republic 2FA via Discord Gateway WebSocket

- **Status**: Accepted
- **Date**: 2026-11-19
- **Deciders**: User, Antigravity Agent
- **Supersedes**: Polling and timeout mechanics in [ADR-0003](./0003-discord-bot-2fa-renewal.md)

## Context

Under ADR-0003, `tr_worker` called `pytr.initiate_weblogin()` first, posted a Discord prompt, and polled `GET /channels/{id}/messages` every `DISCORD_2FA_POLL_INTERVAL_SECONDS` (3s) up to `DISCORD_2FA_TIMEOUT_SECONDS` (300s). This caused two operational issues:

1. **Premature 2FA Timeouts & Expired Login Windows**: Calling `initiate_weblogin()` before the user noticed the Discord notification started Trade Republic's ~120s server-side login countdown while the user was away, and `DISCORD_2FA_TIMEOUT_SECONDS=300` aborted the sync cycle—delaying retry until the next scheduled `TR_POLL_INTERVAL_SECONDS` slot (up to 7 days for weekly schedules).
2. **Continuous REST Polling Noise**: Polling `GET /channels/{id}/messages` every 3 seconds generated continuous HTTP traffic and log noise while waiting for human input.

## Decision

1. **Ready-First Prompting (`tr_worker/session_manager.py`)**:
   - When `resume_websession()` fails or no valid session exists, `TRSessionManager.ensure_authenticated()` prompts the user in Discord *before* calling `pytr.initiate_weblogin()`.
   - Replying with a 4-to-6-digit Authenticator code signals readiness and immediately starts `initiate_weblogin()`, `complete_weblogin(code)`, and (when `status == "PENDING"`) mobile app confirmation (`_await_weblogin_confirmation()`).
2. **Zero-Timeout Discord Gateway WebSocket Subscription (`MESSAGE_CREATE`)**:
   - Remove `DISCORD_2FA_TIMEOUT_SECONDS` and `DISCORD_2FA_POLL_INTERVAL_SECONDS` from `Settings`, `docker-compose.yml`, and `.env.example`.
   - `DiscordClient.get_gateway_url()` fetches the Gateway WSS URL via `GET /gateway/bot`.
   - `Discord2FAClient` connects to Discord Gateway v10 (`?v=10&encoding=json`), handles `OP 10 Hello` + periodic `OP 1` heartbeats (`OP 11` ACKs), identifies via `OP 2 Identify` with `DISCORD_GATEWAY_INTENTS` (`GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT = 37376`), waits for `OP 0 READY`, **then** posts the prompt to Discord and waits indefinitely for `OP 0 MESSAGE_CREATE` (`id > prompt_id`, `channel_id == DISCORD_CHANNEL_ID`, non-bot author, matching 4–6 digit code).
   - Routine `OP 7 Reconnect`, `OP 9 Invalid Session`, and transient socket drops reconnect with exponential backoff (`OP 6 Resume` when resumable) without re-posting the prompt.
   - Non-recoverable Gateway close codes (`4004` auth failed, `4010`–`4014` such as `4014` Disallowed Intents when Message Content Intent is disabled) fail immediately by raising `TRSessionError`.
3. **Indefinite Code-Retry Loop on Login Failure**:
   - If `initiate_weblogin()`, `complete_weblogin(code)`, or `_await_weblogin_confirmation()` fails for any reason, `TRSessionManager` logs a `WARNING` (avoiding redundant `DiscordErrorHandler` tracebacks), resets `self._tr_api = None`, posts the failure reason in Discord, and continues waiting indefinitely for a new Authenticator code after the new prompt message ID.

## Consequences

- **Positive**: Syncs never time out waiting for human 2FA input, Trade Republic's login countdown only starts once the user is actively at Discord with a fresh code, zero REST polling occurs while waiting, and typos/expired codes recover smoothly in-place.
- **Negative / Trade-offs**: Requires Discord **Message Content Intent** to remain enabled on the bot (already documented in `README.md`), and a stuck/unanswered 2FA blocks subsequent scheduled `tr_worker` iterations until the user completes login.
