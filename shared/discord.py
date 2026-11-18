"""Shared Discord REST API v10 client and optional ERROR-level alert handler."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from config.settings import Settings

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_MAX_MESSAGE_LENGTH = 2000


class DiscordAPIError(RuntimeError):
    """Raised when a Discord REST API call fails or returns an invalid payload."""


class DiscordClient:
    """Minimal Discord REST API v10 client for posting and reading channel messages."""

    def __init__(
        self,
        bot_token: str,
        channel_id: str,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.channel_id = channel_id
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=15.0,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
        )
        if http_client is not None:
            self._client.headers.setdefault("Authorization", f"Bot {bot_token}")

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self._client.close()

    def send_message(self, content: str) -> str:
        """Post a message to the configured Discord channel and return its message ID."""
        url = f"{DISCORD_API_BASE}/channels/{self.channel_id}/messages"
        resp = self._client.post(url, json={"content": content})
        if resp.status_code >= 400:
            raise DiscordAPIError(f"Discord send_message failed ({resp.status_code}): {resp.text}")
        data: dict[str, Any] = resp.json()
        msg_id = str(data.get("id") or "")
        if not msg_id:
            raise DiscordAPIError("Discord response missing message id")
        return msg_id

    def get_messages(self, *, after: str, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch recent channel messages posted after `after`."""
        url = f"{DISCORD_API_BASE}/channels/{self.channel_id}/messages"
        resp = self._client.get(url, params={"after": after, "limit": limit})
        if resp.status_code >= 400:
            raise DiscordAPIError(f"Discord get_messages failed ({resp.status_code}): {resp.text}")
        messages: list[dict[str, Any]] = resp.json()
        return messages


class DiscordErrorNotifier:
    """Formats, secret-masks, and dispatches error alerts to Discord."""

    def __init__(
        self,
        client: DiscordClient,
        worker_name: str,
        *,
        dry_run: bool = False,
        use_mock_data: bool = False,
        secrets_to_mask: Sequence[str] = (),
    ) -> None:
        self.client = client
        self.worker_name = worker_name
        self.dry_run = dry_run
        self.use_mock_data = use_mock_data
        self._secrets = tuple(s for s in secrets_to_mask if len(s) >= 6)
        self._formatter = logging.Formatter()

    def close(self) -> None:
        """Close underlying Discord HTTP client resources."""
        self.client.close()

    def _mask_secrets(self, text: str) -> str:
        masked = text
        for secret in self._secrets:
            masked = masked.replace(secret, "***")
        return masked

    def format_alert(self, record: logging.LogRecord) -> str:
        """Format a log record as a secret-masked Discord message within 2000 chars."""
        header = f"🚨 **{self.worker_name} {record.levelname}** (`{record.name}`)\n"
        parts = [record.getMessage()]
        if record.exc_info:
            parts.append(self._formatter.formatException(record.exc_info))
        elif record.exc_text:
            parts.append(record.exc_text)

        raw_body = "\n".join(part for part in parts if part)
        safe_body = self._mask_secrets(raw_body).replace("```", "'''")

        wrapper_prefix = "```\n"
        wrapper_suffix = "\n```"
        trunc_marker = "\n... (truncated)"
        available = (
            DISCORD_MAX_MESSAGE_LENGTH - len(header) - len(wrapper_prefix) - len(wrapper_suffix)
        )
        if available <= len(trunc_marker):
            return (header + safe_body)[:DISCORD_MAX_MESSAGE_LENGTH]

        if len(safe_body) > available:
            safe_body = safe_body[: available - len(trunc_marker)] + trunc_marker

        return f"{header}{wrapper_prefix}{safe_body}{wrapper_suffix}"

    def notify_record(self, record: logging.LogRecord) -> bool:
        """Send a formatted error alert to Discord unless dry-run/mock mode is active."""
        alert_text = self.format_alert(record)
        if self.dry_run or self.use_mock_data:
            logger.info(
                "[DRY_RUN] Would send Discord error alert (%s): %s",
                self.worker_name,
                self._mask_secrets(record.getMessage()),
            )
            return False
        self.client.send_message(alert_text)
        return True


class DiscordErrorHandler(logging.Handler):
    """Logging handler forwarding ERROR+ records to Discord without crashing or recursing."""

    def __init__(self, notifier: DiscordErrorNotifier) -> None:
        super().__init__(level=logging.ERROR)
        self.notifier = notifier
        self._state = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        """Emit an ERROR+ record to Discord with re-entrancy protection."""
        if getattr(self._state, "in_emit", False):
            return
        if record.name == __name__ or record.name.startswith(f"{__name__}."):
            return

        self._state.in_emit = True
        try:
            self.notifier.notify_record(record)
        except Exception:
            self.handleError(record)
        finally:
            self._state.in_emit = False


@contextmanager
def attach_discord_error_handler(
    settings: Settings,
    *,
    worker_name: str,
) -> Iterator[DiscordErrorHandler | None]:
    """Attach an optional ERROR-level Discord handler to the root logger when configured."""
    if not settings.has_discord:
        yield None
        return

    client = DiscordClient(
        bot_token=settings.discord_bot_token.get_secret_value(),
        channel_id=settings.discord_channel_id,
    )
    notifier = DiscordErrorNotifier(
        client=client,
        worker_name=worker_name,
        dry_run=settings.dry_run,
        use_mock_data=settings.use_mock_data,
        secrets_to_mask=settings.secret_values(),
    )
    handler = DiscordErrorHandler(notifier)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        yield handler
    finally:
        root_logger.removeHandler(handler)
        notifier.close()
