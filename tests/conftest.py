"""Shared pytest fixtures for unit, integration, and E2E tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from config.settings import Settings
from pydantic import SecretStr
from shared.database import DeduplicationStore


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    """Return a sanitized Settings instance backed by a temporary directory."""
    return Settings(
        USE_MOCK_DATA=False,
        DRY_RUN=False,
        SQLITE_DB_PATH=tmp_path / "test_finance_sync.db",
        BUDGETBAKERS_BASE_URL="https://rest.budgetbakers.com/wallet",
        BUDGETBAKERS_TOKEN=SecretStr("test-bb-jwt-token"),
        BB_IBKR_ACCOUNT_ID="00000000-0000-0000-0000-000000000001",
        BB_TR_CASH_ACCOUNT_ID="00000000-0000-0000-0000-000000000002",
        BB_TR_WEALTH_ACCOUNT_ID="00000000-0000-0000-0000-000000000003",
        IBKR_FLEX_TOKEN=SecretStr("test-ibkr-token"),
        IBKR_FLEX_QUERY_ID="999999",
        IBKR_FLEX_WAIT_SECONDS=0.0,
        TR_PHONE_NO="+49000000000",
        TR_PIN=SecretStr("0000"),
        TR_SESSION_DIR=tmp_path / "tr_session",
        DISCORD_BOT_TOKEN=SecretStr("test-discord-bot-token"),
        DISCORD_CHANNEL_ID="000000000000000001",
        DISCORD_2FA_TIMEOUT_SECONDS=5.0,
        DISCORD_2FA_POLL_INTERVAL_SECONDS=0.0,
    )


@pytest.fixture
def dedup_store(tmp_path: Path) -> DeduplicationStore:
    """Return an isolated SQLite DeduplicationStore in tmp_path."""
    return DeduplicationStore(tmp_path / "test_finance_sync.db")
