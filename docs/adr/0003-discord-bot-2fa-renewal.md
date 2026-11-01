# ADR 0003: Interactive Discord Bot for Trade Republic 2FA Renewal

## Status
Accepted

## Context
Trade Republic's private API (`pytr`) requires a 4-digit 2FA verification code sent to the user's Trade Republic mobile app or SMS whenever the persistent web session expires. Because `tr_worker` runs headlessly inside a Portainer container, interactive terminal input (`input()`) is inconvenient.

## Decision
Implement an interactive Discord Bot 2FA flow in `tr_worker/session_manager.py`:
1. Persist `pytr` cookies and session metadata in `/app/data`.
2. When a session is missing or expired (`401` / WebSocket auth error), trigger `pytr` web login initiation and post a prompt to `DISCORD_CHANNEL_ID` via Discord REST API v10 (`POST /channels/{channel_id}/messages`).
3. Poll `GET /channels/{channel_id}/messages?after={prompt_message_id}` until a message containing a 4-digit PIN arrives (up to `DISCORD_2FA_TIMEOUT_SECONDS`, default 300s).
4. Complete the `pytr` 2FA login with the received PIN, save the refreshed session to `/app/data`, and post a confirmation message to Discord.

## Consequences
- Headless 2FA renewal can be completed from anywhere via Discord on phone or desktop without SSH/exec access to the homelab server.
