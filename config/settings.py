"""Application settings and category mapping configuration using Pydantic Settings."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.scheduler import parse_start_time, resolve_timezone

# Deterministic BudgetBakers Wallet system category UUIDs
CATEGORY_INVESTMENTS = "5c5c232a-005a-8000-8000-000000000000"
CATEGORY_INTEREST_DIVIDENDS = "5c5c2711-0064-8000-8000-000000000000"
CATEGORY_TRANSFER = "5c5c4e21-00c8-8000-8000-000000000000"
CATEGORY_UNKNOWN_EXPENSE = "5c5c32c9-0082-8000-8000-000000000000"
CATEGORY_GROCERIES = "5c5c03e8-000a-8000-8000-000000000000"
CATEGORY_RESTAURANTS = "5c5c03e9-000a-8000-8000-000000000000"
CATEGORY_BAR_CAFE = "5c5c03ea-000a-8000-8000-000000000000"
CATEGORY_FOOD_DRINKS = "5c5c03eb-000a-8000-8000-000000000000"
CATEGORY_SOFTWARE_GAMES = "5c5c1b5b-0046-8000-8000-000000000000"
CATEGORY_POSTAL = "5c5c1b5c-0046-8000-8000-000000000000"
CATEGORY_INTERNET = "5c5c1b5a-0046-8000-8000-000000000000"
CATEGORY_PHONE = "5c5c1b59-0046-8000-8000-000000000000"
CATEGORY_ACTIVE_SPORT = "5c5c1772-003c-8000-8000-000000000000"
CATEGORY_SPORT_EVENTS = "5c5c1773-003c-8000-8000-000000000000"
CATEGORY_HEALTH_CARE = "5c5c1770-003c-8000-8000-000000000000"
CATEGORY_BOOKS_SUBSCRIPTION = "5c5c1777-003c-8000-8000-000000000000"
CATEGORY_TV_STREAMING = "5c5c1778-003c-8000-8000-000000000000"
CATEGORY_HOLIDAYS_HOTELS = "5c5c1779-003c-8000-8000-000000000000"
CATEGORY_ALCOHOL_TOBACCO = "5c5c177b-003c-8000-8000-000000000000"
CATEGORY_LIFE_ENTERTAINMENT = "5c5c177d-003c-8000-8000-000000000000"
CATEGORY_CLOTHES_SHOES = "5c5c07d0-0014-8000-8000-000000000000"
CATEGORY_HEALTH_BEAUTY = "5c5c07d2-0014-8000-8000-000000000000"
CATEGORY_HOME_GARDEN = "5c5c07d4-0014-8000-8000-000000000000"
CATEGORY_ELECTRONICS = "5c5c07d6-0014-8000-8000-000000000000"
CATEGORY_GIFTS_JOY = "5c5c07d7-0014-8000-8000-000000000000"
CATEGORY_DRUGSTORE = "5c5c07db-0014-8000-8000-000000000000"
CATEGORY_SHOPPING = "5c5c07da-0014-8000-8000-000000000000"
CATEGORY_PUBLIC_TRANSPORT = "5c5c0fa0-0028-8000-8000-000000000000"
CATEGORY_TAXI = "5c5c0fa1-0028-8000-8000-000000000000"
CATEGORY_TRANSPORTATION = "5c5c0fa4-0028-8000-8000-000000000000"
CATEGORY_FUEL = "5c5c1388-0032-8000-8000-000000000000"
CATEGORY_PARKING = "5c5c1389-0032-8000-8000-000000000000"
CATEGORY_CHARGES_FEES = "5c5c1f45-0050-8000-8000-000000000000"

_MERCHANT_SEP_RE = re.compile(r"[-_./']+")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_merchant_text(text: str) -> str:
    """Lowercase text and collapse punctuation separators into single spaces."""
    replaced = _MERCHANT_SEP_RE.sub(" ", text.strip().lower())
    return _WHITESPACE_RE.sub(" ", replaced).strip()


DEFAULT_MERCHANT_CATEGORY_RULES: tuple[tuple[str, str], ...] = (
    # Groceries & Bakeries
    ("supermarket", CATEGORY_GROCERIES),
    ("grocery", CATEGORY_GROCERIES),
    ("market", CATEGORY_GROCERIES),
    ("bakery", CATEGORY_GROCERIES),
    ("pekarn", CATEGORY_GROCERIES),
    ("bakers", CATEGORY_GROCERIES),
    ("lidl", CATEGORY_GROCERIES),
    ("spar", CATEGORY_GROCERIES),
    ("interspar", CATEGORY_GROCERIES),
    ("aldi", CATEGORY_GROCERIES),
    ("hofer", CATEGORY_GROCERIES),
    ("mercator", CATEGORY_GROCERIES),
    ("eurospin", CATEGORY_GROCERIES),
    (" tus ", CATEGORY_GROCERIES),
    ("rewe", CATEGORY_GROCERIES),
    ("edeka", CATEGORY_GROCERIES),
    ("kaufland", CATEGORY_GROCERIES),
    # Restaurants & Fast Food
    ("burger", CATEGORY_RESTAURANTS),
    ("diner", CATEGORY_RESTAURANTS),
    ("restaurant", CATEGORY_RESTAURANTS),
    ("restavracija", CATEGORY_RESTAURANTS),
    ("gostilna", CATEGORY_RESTAURANTS),
    ("pizzeria", CATEGORY_RESTAURANTS),
    ("picerija", CATEGORY_RESTAURANTS),
    ("pizza", CATEGORY_RESTAURANTS),
    ("bistro", CATEGORY_RESTAURANTS),
    ("kebab", CATEGORY_RESTAURANTS),
    ("sushi", CATEGORY_RESTAURANTS),
    ("mcdonalds", CATEGORY_RESTAURANTS),
    ("kfc", CATEGORY_RESTAURANTS),
    ("wolt", CATEGORY_RESTAURANTS),
    ("glovo", CATEGORY_RESTAURANTS),
    # Bars & Cafes
    ("coffee", CATEGORY_BAR_CAFE),
    ("cafe", CATEGORY_BAR_CAFE),
    ("kavarna", CATEGORY_BAR_CAFE),
    ("slascicarna", CATEGORY_BAR_CAFE),
    ("starbucks", CATEGORY_BAR_CAFE),
    (" bar ", CATEGORY_BAR_CAFE),
    (" pub ", CATEGORY_BAR_CAFE),
    # Drugstore & Pharmacy
    ("drugstore", CATEGORY_DRUGSTORE),
    ("pharmacy", CATEGORY_DRUGSTORE),
    ("lekarna", CATEGORY_DRUGSTORE),
    ("apotheke", CATEGORY_DRUGSTORE),
    ("dm drogerie", CATEGORY_DRUGSTORE),
    (" dm ", CATEGORY_DRUGSTORE),
    ("mueller", CATEGORY_DRUGSTORE),
    ("muller", CATEGORY_DRUGSTORE),
    ("rossmann", CATEGORY_DRUGSTORE),
    # Software, Apps, Games & Subscriptions
    ("steam", CATEGORY_SOFTWARE_GAMES),
    ("itch io", CATEGORY_SOFTWARE_GAMES),
    ("playstation", CATEGORY_SOFTWARE_GAMES),
    ("nintendo", CATEGORY_SOFTWARE_GAMES),
    ("openai", CATEGORY_SOFTWARE_GAMES),
    ("chatgpt", CATEGORY_SOFTWARE_GAMES),
    ("anthropic", CATEGORY_SOFTWARE_GAMES),
    ("github", CATEGORY_SOFTWARE_GAMES),
    ("spotify", CATEGORY_TV_STREAMING),
    ("netflix", CATEGORY_TV_STREAMING),
    ("youtube", CATEGORY_TV_STREAMING),
    ("disney plus", CATEGORY_TV_STREAMING),
    ("apple", CATEGORY_SOFTWARE_GAMES),
    ("google", CATEGORY_SOFTWARE_GAMES),
    # Postal
    ("postal", CATEGORY_POSTAL),
    ("parcel", CATEGORY_POSTAL),
    ("posta slovenije", CATEGORY_POSTAL),
    ("dhl", CATEGORY_POSTAL),
    ("dpd", CATEGORY_POSTAL),
    (" gls ", CATEGORY_POSTAL),
    # Sports & Events
    ("intersport", CATEGORY_ACTIVE_SPORT),
    ("decathlon", CATEGORY_ACTIVE_SPORT),
    ("sport", CATEGORY_ACTIVE_SPORT),
    ("fitness", CATEGORY_ACTIVE_SPORT),
    ("gym", CATEGORY_ACTIVE_SPORT),
    ("marathon", CATEGORY_SPORT_EVENTS),
    ("cinema", CATEGORY_LIFE_ENTERTAINMENT),
    ("kino", CATEGORY_LIFE_ENTERTAINMENT),
    ("cineplexx", CATEGORY_LIFE_ENTERTAINMENT),
    # Clothing & Shoes
    ("fashion", CATEGORY_CLOTHES_SHOES),
    ("clothing", CATEGORY_CLOTHES_SHOES),
    ("about you", CATEGORY_CLOTHES_SHOES),
    ("zalando", CATEGORY_CLOTHES_SHOES),
    ("zara", CATEGORY_CLOTHES_SHOES),
    (" h m ", CATEGORY_CLOTHES_SHOES),
    ("primark", CATEGORY_CLOTHES_SHOES),
    ("bershka", CATEGORY_CLOTHES_SHOES),
    ("peek cloppenburg", CATEGORY_CLOTHES_SHOES),
    ("deichmann", CATEGORY_CLOTHES_SHOES),
    # Electronics, Home & Shopping
    ("big bang", CATEGORY_ELECTRONICS),
    ("mimovrste", CATEGORY_SHOPPING),
    ("mediamarkt", CATEGORY_ELECTRONICS),
    ("ikea", CATEGORY_HOME_GARDEN),
    ("obi", CATEGORY_HOME_GARDEN),
    ("bauhaus", CATEGORY_HOME_GARDEN),
    ("merkur", CATEGORY_HOME_GARDEN),
    ("amazon", CATEGORY_SHOPPING),
    ("aliexpress", CATEGORY_SHOPPING),
    ("temu", CATEGORY_SHOPPING),
    ("tedi", CATEGORY_SHOPPING),
    ("kik", CATEGORY_SHOPPING),
    ("pepco", CATEGORY_SHOPPING),
    ("action", CATEGORY_SHOPPING),
    # Transport, Fuel & Travel
    ("fuel", CATEGORY_FUEL),
    ("petrol", CATEGORY_FUEL),
    ("station", CATEGORY_FUEL),
    ("shell", CATEGORY_FUEL),
    (" omv ", CATEGORY_FUEL),
    (" mol ", CATEGORY_FUEL),
    ("parking", CATEGORY_PARKING),
    ("parkirisce", CATEGORY_PARKING),
    ("easypark", CATEGORY_PARKING),
    ("uber", CATEGORY_TAXI),
    ("bolt", CATEGORY_TAXI),
    ("taxi", CATEGORY_TAXI),
    (" lpp ", CATEGORY_PUBLIC_TRANSPORT),
    ("nomago", CATEGORY_PUBLIC_TRANSPORT),
    ("flixbus", CATEGORY_PUBLIC_TRANSPORT),
    ("slovenske zeleznice", CATEGORY_PUBLIC_TRANSPORT),
    ("deutsche bahn", CATEGORY_PUBLIC_TRANSPORT),
    ("ryanair", CATEGORY_HOLIDAYS_HOTELS),
    ("easyjet", CATEGORY_HOLIDAYS_HOTELS),
    ("lufthansa", CATEGORY_HOLIDAYS_HOTELS),
    ("booking com", CATEGORY_HOLIDAYS_HOTELS),
    ("airbnb", CATEGORY_HOLIDAYS_HOTELS),
    ("hotel", CATEGORY_HOLIDAYS_HOTELS),
)

MCC_CATEGORY_MAP: dict[str, str] = {
    # Groceries & Bakeries
    "5411": CATEGORY_GROCERIES,
    "5422": CATEGORY_GROCERIES,
    "5441": CATEGORY_GROCERIES,
    "5451": CATEGORY_GROCERIES,
    "5462": CATEGORY_GROCERIES,
    "5499": CATEGORY_GROCERIES,
    # Dining, Fast Food & Bars
    "5811": CATEGORY_RESTAURANTS,
    "5812": CATEGORY_RESTAURANTS,
    "5814": CATEGORY_RESTAURANTS,
    "5813": CATEGORY_BAR_CAFE,
    # Drugstore, Pharmacy & Health
    "5912": CATEGORY_DRUGSTORE,
    "5977": CATEGORY_HEALTH_BEAUTY,
    "7230": CATEGORY_HEALTH_BEAUTY,
    "8011": CATEGORY_HEALTH_CARE,
    "8021": CATEGORY_HEALTH_CARE,
    "8062": CATEGORY_HEALTH_CARE,
    # Software, Digital Goods, Telecom & Streaming
    "4812": CATEGORY_PHONE,
    "4814": CATEGORY_PHONE,
    "4816": CATEGORY_INTERNET,
    "4899": CATEGORY_TV_STREAMING,
    "5734": CATEGORY_SOFTWARE_GAMES,
    "5815": CATEGORY_BOOKS_SUBSCRIPTION,
    "5816": CATEGORY_SOFTWARE_GAMES,
    "5817": CATEGORY_SOFTWARE_GAMES,
    "5818": CATEGORY_SOFTWARE_GAMES,
    "7372": CATEGORY_SOFTWARE_GAMES,
    # Postal & Financial
    "4215": CATEGORY_POSTAL,
    "9402": CATEGORY_POSTAL,
    "6010": CATEGORY_CHARGES_FEES,
    "6011": CATEGORY_CHARGES_FEES,
    # Clothing & Shoes
    "5611": CATEGORY_CLOTHES_SHOES,
    "5621": CATEGORY_CLOTHES_SHOES,
    "5631": CATEGORY_CLOTHES_SHOES,
    "5641": CATEGORY_CLOTHES_SHOES,
    "5651": CATEGORY_CLOTHES_SHOES,
    "5661": CATEGORY_CLOTHES_SHOES,
    "5691": CATEGORY_CLOTHES_SHOES,
    "5699": CATEGORY_CLOTHES_SHOES,
    # Electronics, Home & General Shopping
    "5732": CATEGORY_ELECTRONICS,
    "5733": CATEGORY_ELECTRONICS,
    "5735": CATEGORY_ELECTRONICS,
    "5200": CATEGORY_HOME_GARDEN,
    "5211": CATEGORY_HOME_GARDEN,
    "5251": CATEGORY_HOME_GARDEN,
    "5261": CATEGORY_HOME_GARDEN,
    "5712": CATEGORY_HOME_GARDEN,
    "5719": CATEGORY_HOME_GARDEN,
    "5310": CATEGORY_SHOPPING,
    "5311": CATEGORY_SHOPPING,
    "5331": CATEGORY_SHOPPING,
    "5399": CATEGORY_SHOPPING,
    "5942": CATEGORY_BOOKS_SUBSCRIPTION,
    "5943": CATEGORY_SHOPPING,
    "5945": CATEGORY_GIFTS_JOY,
    "5947": CATEGORY_GIFTS_JOY,
    "5999": CATEGORY_SHOPPING,
    # Sports, Entertainment & Lodging
    "5941": CATEGORY_ACTIVE_SPORT,
    "7997": CATEGORY_ACTIVE_SPORT,
    "7832": CATEGORY_LIFE_ENTERTAINMENT,
    "7922": CATEGORY_SPORT_EVENTS,
    "7991": CATEGORY_LIFE_ENTERTAINMENT,
    "7999": CATEGORY_LIFE_ENTERTAINMENT,
    "7011": CATEGORY_HOLIDAYS_HOTELS,
    # Transport, Fuel & Parking
    "4111": CATEGORY_PUBLIC_TRANSPORT,
    "4112": CATEGORY_PUBLIC_TRANSPORT,
    "4121": CATEGORY_TAXI,
    "4131": CATEGORY_PUBLIC_TRANSPORT,
    "4511": CATEGORY_HOLIDAYS_HOTELS,
    "4789": CATEGORY_TRANSPORTATION,
    "5541": CATEGORY_FUEL,
    "5542": CATEGORY_FUEL,
    "7523": CATEGORY_PARKING,
}

TR_HINT_CATEGORY_MAP: tuple[tuple[str, str], ...] = (
    ("grocer", CATEGORY_GROCERIES),
    ("supermarket", CATEGORY_GROCERIES),
    ("bakery", CATEGORY_GROCERIES),
    ("restaurant", CATEGORY_RESTAURANTS),
    ("dining", CATEGORY_RESTAURANTS),
    ("fastfood", CATEGORY_RESTAURANTS),
    ("fast food", CATEGORY_RESTAURANTS),
    ("food", CATEGORY_FOOD_DRINKS),
    ("cafe", CATEGORY_BAR_CAFE),
    ("coffee", CATEGORY_BAR_CAFE),
    ("bar", CATEGORY_BAR_CAFE),
    ("nightlife", CATEGORY_BAR_CAFE),
    ("drugstore", CATEGORY_DRUGSTORE),
    ("pharmacy", CATEGORY_DRUGSTORE),
    ("beauty", CATEGORY_HEALTH_BEAUTY),
    ("health", CATEGORY_HEALTH_CARE),
    ("medical", CATEGORY_HEALTH_CARE),
    ("cloth", CATEGORY_CLOTHES_SHOES),
    ("apparel", CATEGORY_CLOTHES_SHOES),
    ("fashion", CATEGORY_CLOTHES_SHOES),
    ("shoe", CATEGORY_CLOTHES_SHOES),
    ("electronic", CATEGORY_ELECTRONICS),
    ("home", CATEGORY_HOME_GARDEN),
    ("garden", CATEGORY_HOME_GARDEN),
    ("furniture", CATEGORY_HOME_GARDEN),
    ("shopping", CATEGORY_SHOPPING),
    ("retail", CATEGORY_SHOPPING),
    ("gift", CATEGORY_GIFTS_JOY),
    ("fuel", CATEGORY_FUEL),
    ("gas", CATEGORY_FUEL),
    ("petrol", CATEGORY_FUEL),
    ("parking", CATEGORY_PARKING),
    ("taxi", CATEGORY_TAXI),
    ("transit", CATEGORY_PUBLIC_TRANSPORT),
    ("public transport", CATEGORY_PUBLIC_TRANSPORT),
    ("transport", CATEGORY_TRANSPORTATION),
    ("mobility", CATEGORY_TRANSPORTATION),
    ("travel", CATEGORY_HOLIDAYS_HOTELS),
    ("hotel", CATEGORY_HOLIDAYS_HOTELS),
    ("flight", CATEGORY_HOLIDAYS_HOTELS),
    ("airline", CATEGORY_HOLIDAYS_HOTELS),
    ("sport", CATEGORY_ACTIVE_SPORT),
    ("fitness", CATEGORY_ACTIVE_SPORT),
    ("entertainment", CATEGORY_LIFE_ENTERTAINMENT),
    ("cinema", CATEGORY_LIFE_ENTERTAINMENT),
    ("streaming", CATEGORY_TV_STREAMING),
    ("subscription", CATEGORY_BOOKS_SUBSCRIPTION),
    ("software", CATEGORY_SOFTWARE_GAMES),
    ("gaming", CATEGORY_SOFTWARE_GAMES),
    ("alcohol", CATEGORY_ALCOHOL_TOBACCO),
    ("tobacco", CATEGORY_ALCOHOL_TOBACCO),
    ("postal", CATEGORY_POSTAL),
    ("telecom", CATEGORY_PHONE),
    ("internet", CATEGORY_INTERNET),
    ("fee", CATEGORY_CHARGES_FEES),
)


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
    sync_start_time: time | None = Field(
        default=None,
        alias="SYNC_START_TIME",
    )
    sync_timezone: str | None = Field(
        default=None,
        alias="SYNC_TIMEZONE",
    )

    @field_validator("sync_start_time", mode="before")
    @classmethod
    def _validate_sync_start_time(cls, value: str | time | None) -> time | None:
        return parse_start_time(value)

    @field_validator("sync_timezone", mode="before")
    @classmethod
    def _validate_sync_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        try:
            resolve_timezone(stripped)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Invalid SYNC_TIMEZONE {value!r}: {exc}") from exc
        return stripped

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

    # BudgetBakers CouchDB (Native Stock Holdings Sync)
    bb_couchdb_url: str = Field(
        default="https://couch-prod-eu-2.budgetbakers.com",
        alias="BB_COUCHDB_URL",
    )
    bb_couchdb_user: str = Field(
        default="",
        alias="BB_COUCHDB_USER",
    )
    bb_couchdb_password: SecretStr = Field(
        default=SecretStr(""),
        alias="BB_COUCHDB_PASSWORD",
    )

    @property
    def has_couchdb(self) -> bool:
        """Return True when CouchDB stock holdings sync credentials are configured."""
        return bool(self.bb_couchdb_user.strip() and self.bb_couchdb_password.get_secret_value())

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
    ibkr_zero_cost_baseline_assets: str = Field(
        default="IBKR",
        alias="IBKR_ZERO_COST_BASELINE_ASSETS",
    )

    @property
    def zero_cost_baseline_asset_set(self) -> frozenset[str]:
        """Return uppercase symbols/ISINs whose IBKR baseline shares have zero cost."""
        return frozenset(
            item.strip().upper()
            for item in self.ibkr_zero_cost_baseline_assets.split(",")
            if item.strip()
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

    # Discord Bot (2FA Renewal & Optional Error Notifications)
    discord_bot_token: SecretStr = Field(
        default=SecretStr("mock-discord-bot-token"),
        alias="DISCORD_BOT_TOKEN",
    )
    discord_channel_id: str = Field(
        default="000000000000000000",
        alias="DISCORD_CHANNEL_ID",
    )

    @property
    def has_discord(self) -> bool:
        """Return True when non-placeholder Discord Bot credentials are configured."""
        bot_secret = self.discord_bot_token.get_secret_value().strip()
        channel = self.discord_channel_id.strip()
        default_placeholder = "mock-discord-bot-token"
        return bool(
            bot_secret
            and bot_secret != default_placeholder
            and channel
            and channel != "000000000000000000"
        )

    def secret_values(self) -> tuple[str, ...]:
        """Return configured secret strings for masking in logs and alerts."""
        candidates = (
            self.budgetbakers_token.get_secret_value(),
            self.bb_couchdb_password.get_secret_value(),
            self.ibkr_flex_token.get_secret_value(),
            self.discord_bot_token.get_secret_value(),
            self.tr_pin.get_secret_value(),
        )
        return tuple(val for val in candidates if val and len(val) >= 6)

    def resolve_merchant_category(
        self,
        merchant_name: str,
        mcc: str | None = None,
        tr_hint: str | None = None,
    ) -> str:
        """Resolve merchant keywords, MCC, or Trade Republic hints to a BudgetBakers category."""
        normalized = _normalize_merchant_text(merchant_name)
        padded = f" {normalized} "
        for keyword, category_id in DEFAULT_MERCHANT_CATEGORY_RULES:
            if keyword.startswith(" ") or keyword.endswith(" "):
                if keyword in padded:
                    return category_id
            elif keyword in normalized:
                return category_id

        if mcc:
            clean_mcc = mcc.strip()
            if clean_mcc in MCC_CATEGORY_MAP:
                return MCC_CATEGORY_MAP[clean_mcc]

        if tr_hint:
            norm_hint = _normalize_merchant_text(tr_hint)
            for hint_key, category_id in TR_HINT_CATEGORY_MAP:
                if hint_key in norm_hint:
                    return category_id

        return self.bb_category_unknown_expense


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
