"""Unit and integration tests for shared.discord and Discord error alerting."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx
from config.settings import Settings
from pydantic import SecretStr
from shared.discord import (
    DISCORD_API_BASE,
    DISCORD_MAX_MESSAGE_LENGTH,
    DiscordClient,
    DiscordErrorHandler,
    DiscordErrorNotifier,
    attach_discord_error_handler,
)


def test_settings_has_discord_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Settings.has_discord detects real vs default/blank Discord settings."""
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
    assert Settings().has_discord is False

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "")
    assert Settings().has_discord is False

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "real-bot-token")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "000000000000000000")
    assert Settings().has_discord is False

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "real-bot-token")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "000000000000000001")
    assert Settings().has_discord is True


@respx.mock
def test_discord_error_handler_masks_secrets_truncates_and_sends_every_error() -> None:
    """Verify DiscordErrorHandler posts masked, <=2000-char alerts on every ERROR log."""
    channel_id = "000000000000000001"
    secret_tok = "super-secret-bb-token-value"
    route = respx.post(f"{DISCORD_API_BASE}/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg-1"})
    )

    notifier = DiscordErrorNotifier(
        client=DiscordClient(bot_token="bot-token-secret", channel_id=channel_id),
        worker_name="IBKR",
        secrets_to_mask=(secret_tok, "bot-token-secret"),
    )
    handler = DiscordErrorHandler(notifier)
    test_logger = logging.getLogger("tests.discord_alert_unit")
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)
    test_logger.propagate = False
    try:
        # INFO/WARNING must not trigger Discord
        test_logger.info("regular info")
        test_logger.warning("non-fatal warning")
        assert route.call_count == 0

        # ERROR with secret and very long message triggers masked, capped post
        long_detail = "x" * 2500
        test_logger.error("Failed using token=%s detail=%s", secret_tok, long_detail)
        assert route.call_count == 1
        sent_1 = route.calls[0].request.content.decode("utf-8")
        assert secret_tok not in sent_1
        assert "***" in sent_1
        import json

        payload_1 = json.loads(sent_1)
        assert len(payload_1["content"]) <= DISCORD_MAX_MESSAGE_LENGTH
        assert "IBKR" in payload_1["content"]

        # Repeated identical error also notifies (no deduplication/cooldown)
        try:
            raise RuntimeError(f"boom with {secret_tok}")
        except RuntimeError:
            test_logger.exception("Unhandled sync crash")
        assert route.call_count == 2
        payload_2 = json.loads(route.calls[1].request.content)
        assert secret_tok not in payload_2["content"]
        assert "RuntimeError: boom with ***" in payload_2["content"]
        assert len(payload_2["content"]) <= DISCORD_MAX_MESSAGE_LENGTH
    finally:
        test_logger.removeHandler(handler)
        notifier.close()


@respx.mock
def test_discord_error_handler_suppressed_in_dry_run_and_mock_mode() -> None:
    """Verify live Discord alerts are suppressed when dry_run=True or use_mock_data=True."""
    channel_id = "000000000000000001"
    route = respx.post(f"{DISCORD_API_BASE}/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg-1"})
    )

    for dry_run, use_mock in ((True, False), (False, True)):
        notifier = DiscordErrorNotifier(
            client=DiscordClient(bot_token="real-bot-token", channel_id=channel_id),
            worker_name="Trade Republic",
            dry_run=dry_run,
            use_mock_data=use_mock,
        )
        handler = DiscordErrorHandler(notifier)
        test_logger = logging.getLogger("tests.discord_dry_run")
        test_logger.setLevel(logging.INFO)
        test_logger.addHandler(handler)
        test_logger.propagate = False
        try:
            test_logger.error("Something went wrong")
        finally:
            test_logger.removeHandler(handler)
            notifier.close()

    assert route.call_count == 0


@respx.mock
def test_discord_error_handler_survives_discord_failure_without_recursion() -> None:
    """Verify Discord API failure does not raise or recurse infinitely."""
    channel_id = "000000000000000001"
    route = respx.post(f"{DISCORD_API_BASE}/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    notifier = DiscordErrorNotifier(
        client=DiscordClient(bot_token="real-bot-token", channel_id=channel_id),
        worker_name="IBKR",
    )
    handler = DiscordErrorHandler(notifier)
    test_logger = logging.getLogger("tests.discord_failure")
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)
    test_logger.propagate = False
    try:
        # Must not raise even though Discord returned 500
        test_logger.error("Sync failed")
        assert route.call_count == 1
    finally:
        test_logger.removeHandler(handler)
        notifier.close()


@respx.mock
def test_attach_discord_error_handler_respects_optional_config(
    test_settings: Settings,
) -> None:
    """Verify attach_discord_error_handler attaches only when has_discord is True."""
    unconfigured = test_settings.model_copy(
        update={
            "discord_bot_token": SecretStr("mock-discord-bot-token"),
            "discord_channel_id": "000000000000000000",
        }
    )
    with attach_discord_error_handler(unconfigured, worker_name="IBKR") as handler:
        assert handler is None

    channel_id = "000000000000000001"
    route = respx.post(f"{DISCORD_API_BASE}/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg-99"})
    )
    configured = test_settings.model_copy(
        update={
            "use_mock_data": False,
            "dry_run": False,
            "discord_bot_token": SecretStr("real-bot-token"),
            "discord_channel_id": channel_id,
        }
    )
    with attach_discord_error_handler(configured, worker_name="IBKR") as handler:
        assert handler is not None
        logging.getLogger("tests.root_attach").error("Root error test")

    assert route.call_count == 1
