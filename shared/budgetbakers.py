"""REST client for BudgetBakers Wallet API (https://rest.budgetbakers.com/wallet/v1/api/)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

MAX_RECORDS_BATCH_SIZE = 20
MAX_DELETE_BATCH_SIZE = 10


class BudgetBakersAPIError(RuntimeError):
    """Raised when the BudgetBakers Wallet API returns an unrecoverable error."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"BudgetBakers API error ({status_code}): {message}")
        self.status_code = status_code
        self.message = message


class AmountPayload(BaseModel):
    """Monetary amount with ISO-4217 currency code."""

    model_config = ConfigDict(populate_by_name=True)

    value: float
    currency_code: str = Field(default="EUR", serialization_alias="currencyCode")


class CreateRecordPayload(BaseModel):
    """Single transaction record payload for POST /v1/api/records."""

    model_config = ConfigDict(populate_by_name=True)

    account_id: str = Field(serialization_alias="accountId")
    amount: AmountPayload
    category_id: str = Field(serialization_alias="categoryId")
    record_date: str = Field(serialization_alias="recordDate")
    counter_party: str | None = Field(default=None, serialization_alias="counterParty")
    note: str | None = Field(default=None)
    record_state: str = Field(default="cleared", serialization_alias="recordState")

    def to_api_dict(self) -> dict[str, Any]:
        """Serialize to exact BudgetBakers OpenAPI dictionary format."""
        return self.model_dump(by_alias=True, exclude_none=True)


def format_stock_buy_note(
    symbol: str,
    isin: str,
    shares: float | Decimal,
    price: float | Decimal,
    currency: str = "EUR",
    side: str = "BUY",
) -> str:
    """Format stock trade note embedding executed shares, ISIN, and price."""
    clean_side = side.strip().upper() or "BUY"
    clean_symbol = symbol.strip()
    clean_isin = isin.strip().upper()
    shares_val = float(shares)
    price_val = float(price)
    note = (
        f"{clean_side} {shares_val:.4f} sh {clean_isin} "
        f"({clean_symbol}) @ {price_val:.2f} {currency}"
    )
    return note[:255]


class BudgetBakersClient:
    """HTTP client for BudgetBakers Wallet REST API."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://rest.budgetbakers.com/wallet",
        *,
        dry_run: bool = False,
        timeout: float = 20.0,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_prefix = f"{self.base_url}/v1/api"
        self.dry_run = dry_run
        self._sleep = sleep_fn
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        if http_client is not None:
            self._client.headers.setdefault("Authorization", f"Bearer {token}")
            self._client.headers.setdefault("Accept", "application/json")

    def close(self) -> None:
        """Close the underlying HTTP client if owned by this instance."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BudgetBakersClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        max_retries: int = 3,
    ) -> Any:
        url = f"{self.api_prefix}/{path.lstrip('/')}"
        for attempt in range(max_retries):
            response = self._client.request(method, url, params=params, json=json_body)
            if response.status_code == 429 and attempt < max_retries - 1:
                retry_after = float(response.headers.get("Retry-After", "5"))
                logger.warning(
                    "Rate limited (429) on %s %s; waiting %.1fs", method, path, retry_after
                )
                self._sleep(retry_after)
                continue
            if response.status_code >= 400:
                raise BudgetBakersAPIError(response.status_code, response.text)
            if not response.content:
                return {}
            return response.json()
        raise BudgetBakersAPIError(429, "Rate limit retries exhausted")

    def create_records(self, records: Sequence[CreateRecordPayload]) -> list[str]:
        """Push records via POST /v1/api/records in chunks of at most 20 items."""
        if not records:
            return []
        if self.dry_run:
            logger.info("[DRY_RUN] Would create %d BudgetBakers records", len(records))
            return [f"dry-run-{idx}" for idx in range(len(records))]

        created_ids: list[str] = []
        for start in range(0, len(records), MAX_RECORDS_BATCH_SIZE):
            batch = records[start : start + MAX_RECORDS_BATCH_SIZE]
            payload = [item.to_api_dict() for item in batch]
            data: dict[str, Any] = self._request("POST", "/records", json_body=payload)
            for res in data.get("results", []):
                if res.get("success") and res.get("id"):
                    created_ids.append(str(res["id"]))
                elif not res.get("success"):
                    err_msg = str(res.get("error", "Unknown record creation error"))
                    raise BudgetBakersAPIError(207, err_msg)
        return created_ids

    def get_account(self, account_id: str) -> dict[str, Any]:
        """Fetch a single account's details from GET /v1/api/accounts."""
        if self.dry_run:
            return {
                "id": account_id,
                "recordStats": {"totalIncomes": 0.0, "totalExpenses": 0.0},
            }
        data: dict[str, Any] = self._request("GET", "/accounts", params={"id": account_id})
        accounts: list[dict[str, Any]] = data.get("accounts", [])
        for acc in accounts:
            if acc.get("id") == account_id:
                return acc
        raise BudgetBakersAPIError(404, f"Account {account_id} not found")

    def patch_account_initial_balance(
        self,
        account_id: str,
        initial_balance: float,
    ) -> dict[str, Any]:
        """Send PATCH /v1/api/accounts to update an account's initialBalance."""
        rounded = round(float(initial_balance), 2)
        if self.dry_run:
            logger.info(
                "[DRY_RUN] Would PATCH account %s initialBalance=%.2f",
                account_id,
                rounded,
            )
            return {"id": account_id, "initialBalance": rounded, "dryRun": True}

        payload = [{"id": account_id, "initialBalance": rounded}]
        res: dict[str, Any] = self._request("PATCH", "/accounts", json_body=payload)
        return res

    def update_account_target_balance(
        self,
        account_id: str,
        target_balance: float,
    ) -> float:
        """Compute initialBalance so currentBalance == target_balance and PATCH the account.

        Formula in BudgetBakers: currentBalance = initial + totalIncomes - totalExpenses
        => initialBalance = target_balance - totalIncomes + totalExpenses
        """
        account = self.get_account(account_id)
        stats: dict[str, Any] = account.get("recordStats") or {}
        total_incomes = float(stats.get("totalIncomes") or 0.0)
        total_expenses = float(stats.get("totalExpenses") or 0.0)
        initial_balance = round(float(target_balance) - total_incomes + total_expenses, 2)
        self.patch_account_initial_balance(account_id, initial_balance)
        return initial_balance

    def get_records(
        self,
        account_id: str,
        *,
        category_id: str | None = None,
        record_date_gte: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Query existing records via GET /v1/api/records."""
        if self.dry_run:
            return []
        params: dict[str, Any] = {
            "accountId": account_id,
            "limit": min(max(limit, 1), 200),
        }
        if category_id:
            params["categoryId"] = category_id
        if record_date_gte:
            params["recordDate"] = f"gte.{record_date_gte}"
        data: dict[str, Any] = self._request("GET", "/records", params=params)
        records: list[dict[str, Any]] = data.get("records", [])
        return records

    def delete_records(self, record_ids: Sequence[str]) -> int:
        """Delete records via DELETE /v1/api/records in chunks of at most 10 IDs."""
        if not record_ids:
            return 0
        if self.dry_run:
            logger.info("[DRY_RUN] Would delete %d BudgetBakers records", len(record_ids))
            return len(record_ids)

        deleted_count = 0
        for start in range(0, len(record_ids), MAX_DELETE_BATCH_SIZE):
            batch = list(record_ids[start : start + MAX_DELETE_BATCH_SIZE])
            data: dict[str, Any] = self._request(
                "DELETE",
                "/records",
                json_body={"ids": batch},
            )
            summary: dict[str, Any] = data.get("summary") or {}
            deleted_count += int(summary.get("succeeded", len(batch)))
        return deleted_count
