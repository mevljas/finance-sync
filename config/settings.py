"""Application settings and category mapping configuration using Pydantic Settings."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Deterministic BudgetBakers Wallet system category UUIDs
CATEGORY_INVESTMENTS = "5c5c232a-005a-8000-8000-000000000000"
CATEGORY_INTEREST_DIVIDENDS = "5c5c2711-0064-8000-8000-000000000000"
CATEGORY_TRANSFER = "5c5c4e21-00c8-8000-8000-000000000000"
CATEGORY_UNKNOWN_EXPENSE = "5c5c32c9-0082-8000-8000-000000000000"
CATEGORY_GROCERIES = "5c5c03e8-000a-8000-8000-000000000000"
CATEGORY_RESTAURANTS = "5c5c03e9-000a-8000-8000-000000000000"
CATEGORY_BAR_CAFE = "5c5c03ea-000a-8000-8000-000000000000"
CATEGORY_SOFTWARE_GAMES = "5c5c1b5b-0046-8000-8000-000000000000"
CATEGORY_POSTAL = "5c5c1b5c-0046-8000-8000-000000000000"
CATEGORY_ACTIVE_SPORT = "5c5c1772-003c-8000-8000-000000000000"
CATEGORY_SPORT_EVENTS = "5c5c1773-003c-8000-8000-000000000000"
CATEGORY_CLOTHES_SHOES = "5c5c07d0-0014-8000-8000-000000000000"
CATEGORY_DRUGSTORE = "5c5c07db-0014-8000-8000-000000000000"
CATEGORY_SHOPPING = "5c5c07da-0014-8000-8000-000000000000"
CATEGORY_FUEL = "5c5c1388-0032-8000-8000-000000000000"

DEFAULT_MERCHANT_CATEGORY_RULES: tuple[tuple[str, str], ...] = (
    ("supermarket", CATEGORY_GROCERIES),
    ("grocery", CATEGORY_GROCERIES),
    ("market", CATEGORY_GROCERIES),
    ("bakery", CATEGORY_GROCERIES),
    ("lidl", CATEGORY_GROCERIES),
    ("spar", CATEGORY_GROCERIES),
    ("aldi", CATEGORY_GROCERIES),
    ("burger", CATEGORY_RESTAURANTS),
    ("diner", CATEGORY_RESTAURANTS),
    ("restaurant", CATEGORY_RESTAURANTS),
    ("pizzeria", CATEGORY_RESTAURANTS),
    ("pizza", CATEGORY_RESTAURANTS),
    ("bistro", CATEGORY_RESTAURANTS),
    ("mcdonalds", CATEGORY_RESTAURANTS),
    ("wolt", CATEGORY_RESTAURANTS),
    ("coffee", CATEGORY_BAR_CAFE),
    ("cafe", CATEGORY_BAR_CAFE),
    ("bar ", CATEGORY_BAR_CAFE),
    ("pub", CATEGORY_BAR_CAFE),
    ("steam", CATEGORY_SOFTWARE_GAMES),
    ("itch.io", CATEGORY_SOFTWARE_GAMES),
    ("postal", CATEGORY_POSTAL),
    ("parcel", CATEGORY_POSTAL),
    ("gls", CATEGORY_POSTAL),
    ("intersport", CATEGORY_ACTIVE_SPORT),
    ("decathlon", CATEGORY_ACTIVE_SPORT),
    ("sport", CATEGORY_ACTIVE_SPORT),
    ("marathon", CATEGORY_SPORT_EVENTS),
    ("fashion", CATEGORY_CLOTHES_SHOES),
    ("clothing", CATEGORY_CLOTHES_SHOES),
    ("about you", CATEGORY_CLOTHES_SHOES),
    ("zara", CATEGORY_CLOTHES_SHOES),
    ("drugstore", CATEGORY_DRUGSTORE),
    ("pharmacy", CATEGORY_DRUGSTORE),
    ("dm drogerie", CATEGORY_DRUGSTORE),
    ("mueller", CATEGORY_DRUGSTORE),
    ("fuel", CATEGORY_FUEL),
    ("petrol", CATEGORY_FUEL),
    ("station", CATEGORY_FUEL),
    ("shell", CATEGORY_FUEL),
)

MCC_CATEGORY_MAP: dict[str, str] = {
    "5411": CATEGORY_GROCERIES,
    "5422": CATEGORY_GROCERIES,
    "5499": CATEGORY_GROCERIES,
    "5812": CATEGORY_RESTAURANTS,
    "5814": CATEGORY_RESTAURANTS,
    "5813": CATEGORY_BAR_CAFE,
    "5734": CATEGORY_SOFTWARE_GAMES,
    "5816": CATEGORY_SOFTWARE_GAMES,
    "4215": CATEGORY_POSTAL,
    "9402": CATEGORY_POSTAL,
    "5651": CATEGORY_CLOTHES_SHOES,
    "5691": CATEGORY_CLOTHES_SHOES,
    "5941": CATEGORY_ACTIVE_SPORT,
    "5541": CATEGORY_FUEL,
    "5542": CATEGORY_FUEL,
}


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Execution & Storage
    use_mock_data: bool = Field(default=False, alias="USE_MOCK_DATA")
    dry_run: bool = Field(default=False, alias="DRY_RUN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    sqlite_db_path: Path = Field(
        default=Path("/app/data/finance_sync.db"),
        alias="SQLITE_DB_PATH",
    )

    # BudgetBakers Wallet API
    budgetbakers_base_url: str = Field(
        default="https://rest.budgetbakers.com/wallet",
        alias="BUDGETBAKERS_BASE_URL",
    )
    budgetbakers_token: SecretStr = Field(
        default=SecretStr("mock-budgetbakers-token"),
        alias="BUDGETBAKERS_TOKEN",
    )
    bb_ibkr_account_id: str = Field(
        default="00000000-0000-0000-0000-000000000001",
        alias="BB_IBKR_ACCOUNT_ID",
    )
    bb_tr_cash_account_id: str = Field(
        default="00000000-0000-0000-0000-000000000002",
        alias="BB_TR_CASH_ACCOUNT_ID",
    )
    bb_tr_wealth_account_id: str = Field(
        default="00000000-0000-0000-0000-000000000003",
        alias="BB_TR_WEALTH_ACCOUNT_ID",
    )

    # BudgetBakers Category UUIDs
    bb_category_investments: str = Field(
        default=CATEGORY_INVESTMENTS,
        alias="BB_CATEGORY_INVESTMENTS",
    )
    bb_category_interest_dividends: str = Field(
        default=CATEGORY_INTEREST_DIVIDENDS,
        alias="BB_CATEGORY_INTEREST_DIVIDENDS",
    )
    bb_category_transfer: str = Field(
        default=CATEGORY_TRANSFER,
        alias="BB_CATEGORY_TRANSFER",
    )
    bb_category_unknown_expense: str = Field(
        default=CATEGORY_UNKNOWN_EXPENSE,
        alias="BB_CATEGORY_UNKNOWN_EXPENSE",
    )

    # Interactive Brokers Flex Web Service
    ibkr_flex_token: SecretStr = Field(
        default=SecretStr("mock-ibkr-flex-token"),
        alias="IBKR_FLEX_TOKEN",
    )
    ibkr_flex_query_id: str = Field(
        default="000000",
        alias="IBKR_FLEX_QUERY_ID",
    )
    ibkr_poll_interval_seconds: int = Field(
        default=21600,
        alias="IBKR_POLL_INTERVAL_SECONDS",
    )
    ibkr_flex_wait_seconds: float = Field(
        default=10.0,
        alias="IBKR_FLEX_WAIT_SECONDS",
    )

    # Trade Republic Private API
    tr_phone_no: str = Field(
        default="+49000000000",
        alias="TR_PHONE_NO",
    )
    tr_pin: SecretStr = Field(
        default=SecretStr("0000"),
        alias="TR_PIN",
    )
    tr_session_dir: Path = Field(
        default=Path("/app/data"),
        alias="TR_SESSION_DIR",
    )
    tr_poll_interval_seconds: int = Field(
        default=900,
        alias="TR_POLL_INTERVAL_SECONDS",
    )
    tr_card_cutoff_date: datetime = Field(
        default=datetime(2026, 11, 1, 9, 0, 0, tzinfo=UTC),
        alias="TR_CARD_CUTOFF_DATE",
    )

    # Discord Bot (2FA Renewal)
    discord_bot_token: SecretStr = Field(
        default=SecretStr("mock-discord-bot-token"),
        alias="DISCORD_BOT_TOKEN",
    )
    discord_channel_id: str = Field(
        default="000000000000000000",
        alias="DISCORD_CHANNEL_ID",
    )
    discord_2fa_timeout_seconds: float = Field(
        default=300.0,
        alias="DISCORD_2FA_TIMEOUT_SECONDS",
    )
    discord_2fa_poll_interval_seconds: float = Field(
        default=3.0,
        alias="DISCORD_2FA_POLL_INTERVAL_SECONDS",
    )

    def resolve_merchant_category(
        self,
        merchant_name: str,
        mcc: str | None = None,
    ) -> str:
        """Resolve a merchant name or MCC code to a BudgetBakers category UUID."""
        normalized = merchant_name.strip().lower()
        for keyword, category_id in DEFAULT_MERCHANT_CATEGORY_RULES:
            if keyword in normalized:
                return category_id
        if mcc and mcc in MCC_CATEGORY_MAP:
            return MCC_CATEGORY_MAP[mcc]
        return self.bb_category_unknown_expense


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
