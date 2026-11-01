"""End-to-end stateful pipeline test verifying IBKR + TR workers and CLI entrypoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from config.settings import get_settings
from ibkr_worker.worker import main as ibkr_main
from tr_worker.worker import main as tr_main


class StatefulWalletMock:
    """In-memory stateful BudgetBakers Wallet server validating exact balance formulas."""

    def __init__(self, ibkr_acc_id: str, tr_cash_acc_id: str, tr_wealth_acc_id: str) -> None:
        self.accounts: dict[str, dict[str, Any]] = {
            ibkr_acc_id: {
                "id": ibkr_acc_id,
                "name": "Demo IBKR Investment",
                "initial": 5000.00,
                "records": [
                    {
                        "id": "demo-transfer-1",
                        "accountId": ibkr_acc_id,
                        "amount": {"value": 1000.00, "currencyCode": "EUR"},
                        "categoryId": "5c5c4e21-00c8-8000-8000-000000000000",
                        "recordDate": "2026-10-01T12:00:00Z",
                        "note": "Demo deposit transfer",
                    }
                ],
            },
            tr_cash_acc_id: {
                "id": tr_cash_acc_id,
                "name": "Demo Trade Republic Cash",
                "initial": 2000.00,
                "records": [
                    {
                        "id": "manual-sav-1",
                        "accountId": tr_cash_acc_id,
                        "amount": {"value": -50.00, "currencyCode": "EUR"},
                        "categoryId": "5c5c232a-005a-8000-8000-000000000000",
                        "recordDate": "2026-10-16T10:00:00Z",
                        "note": "Savings plan",
                    },
                    {
                        "id": "manual-int-1",
                        "accountId": tr_cash_acc_id,
                        "amount": {"value": 100.00, "currencyCode": "EUR"},
                        "categoryId": "5c5c2711-0064-8000-8000-000000000000",
                        "recordDate": "2026-11-01T08:00:00Z",
                        "note": "Interest",
                    },
                ],
            },
            tr_wealth_acc_id: {
                "id": tr_wealth_acc_id,
                "name": "Demo Trade Republic Wealth",
                "initial": 500.00,
                "records": [],
            },
        }
        self._seq = 1

    def compute_account_view(self, acc_id: str) -> dict[str, Any]:
        acc = self.accounts[acc_id]
        incomes = sum(
            float(r["amount"]["value"]) for r in acc["records"] if float(r["amount"]["value"]) > 0
        )
        expenses = sum(
            abs(float(r["amount"]["value"]))
            for r in acc["records"]
            if float(r["amount"]["value"]) < 0
        )
        initial = float(acc["initial"])
        current = round(initial + incomes - expenses, 2)
        return {
            "id": acc_id,
            "name": acc["name"],
            "recordStats": {
                "recordCount": len(acc["records"]),
                "totalIncomes": round(incomes, 2),
                "totalExpenses": round(expenses, 2),
            },
            "balance": {
                "initial": initial,
                "currentBalance": current,
                "rawCurrentBalance": current,
                "formula": "initial + totalIncomes - totalExpenses",
            },
        }

    def handle_get_accounts(self, request: httpx.Request) -> httpx.Response:
        target_id = request.url.params.get("id")
        if target_id and target_id in self.accounts:
            return httpx.Response(200, json={"accounts": [self.compute_account_view(target_id)]})
        return httpx.Response(
            200,
            json={"accounts": [self.compute_account_view(aid) for aid in self.accounts]},
        )

    def handle_patch_accounts(self, request: httpx.Request) -> httpx.Response:
        items: list[dict[str, Any]] = json.loads(request.content)
        results = []
        for item in items:
            aid = str(item["id"])
            if "initialBalance" in item:
                self.accounts[aid]["initial"] = float(item["initialBalance"])
            results.append(
                {
                    "id": aid,
                    "success": True,
                    "account": self.compute_account_view(aid),
                }
            )
        return httpx.Response(200, json={"results": results})

    def handle_get_records(self, request: httpx.Request) -> httpx.Response:
        acc_id = request.url.params.get("accountId", "")
        cat_id = request.url.params.get("categoryId")
        records = list(self.accounts.get(acc_id, {}).get("records", []))
        if cat_id:
            records = [r for r in records if r.get("categoryId") == cat_id]
        return httpx.Response(200, json={"records": records})

    def handle_post_records(self, request: httpx.Request) -> httpx.Response:
        items: list[dict[str, Any]] = json.loads(request.content)
        assert len(items) <= 20, "Batch size exceeded 20!"
        results = []
        for idx, item in enumerate(items):
            rec_id = f"gen-rec-{self._seq}"
            self._seq += 1
            stored = {"id": rec_id, **item}
            self.accounts[item["accountId"]]["records"].append(stored)
            results.append({"id": rec_id, "inputIndex": idx, "success": True})
        return httpx.Response(200, json={"results": results})

    def handle_delete_records(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        ids_to_del = set(body.get("ids", []))
        assert len(ids_to_del) <= 10, "Delete batch size exceeded 10!"
        removed = 0
        for acc in self.accounts.values():
            before = len(acc["records"])
            acc["records"] = [r for r in acc["records"] if r["id"] not in ids_to_del]
            removed += before - len(acc["records"])
        return httpx.Response(200, json={"summary": {"succeeded": removed}})


@respx.mock
def test_full_e2e_pipeline_both_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run both ibkr_worker and tr_worker CLI entrypoints against stateful Wallet mock."""
    ibkr_acc = "11111111-1111-4111-8111-111111111111"
    tr_cash_acc = "22222222-2222-4222-8222-222222222222"
    tr_wealth_acc = "33333333-3333-4333-8333-333333333333"
    db_file = tmp_path / "shared_e2e.db"

    monkeypatch.setenv("USE_MOCK_DATA", "true")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SQLITE_DB_PATH", str(db_file))
    monkeypatch.setenv("BUDGETBAKERS_TOKEN", "e2e-jwt-token")
    monkeypatch.setenv("BB_IBKR_ACCOUNT_ID", ibkr_acc)
    monkeypatch.setenv("BB_TR_CASH_ACCOUNT_ID", tr_cash_acc)
    monkeypatch.setenv("BB_TR_WEALTH_ACCOUNT_ID", tr_wealth_acc)
    monkeypatch.setenv("TR_SESSION_DIR", str(tmp_path / "tr_sess"))
    get_settings.cache_clear()

    wallet = StatefulWalletMock(ibkr_acc, tr_cash_acc, tr_wealth_acc)
    respx.get("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        side_effect=wallet.handle_get_accounts
    )
    respx.patch("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        side_effect=wallet.handle_patch_accounts
    )
    respx.get("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        side_effect=wallet.handle_get_records
    )
    respx.post("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        side_effect=wallet.handle_post_records
    )
    respx.delete("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        side_effect=wallet.handle_delete_records
    )

    # 1. Run IBKR worker (--once) and TR worker (--once)
    assert ibkr_main(["--once"]) == 0
    assert tr_main(["--once"]) == 0

    # 2. Verify final computed balances in Wallet match exact live broker valuations
    ibkr_view = wallet.compute_account_view(ibkr_acc)
    tr_wealth_view = wallet.compute_account_view(tr_wealth_acc)

    # IBKR OpenPositions (10000.00) + endingCash (100.00) = 10100.00 EUR
    assert ibkr_view["balance"]["currentBalance"] == 10100.00
    # TR Portfolio netValue = 1000.00 EUR
    assert tr_wealth_view["balance"]["currentBalance"] == 1000.00

    # 3. Re-run both workers and verify idempotency (record counts do not increase)
    ibkr_count_before = ibkr_view["recordStats"]["recordCount"]
    tr_wealth_count_before = tr_wealth_view["recordStats"]["recordCount"]
    tr_cash_count_before = wallet.compute_account_view(tr_cash_acc)["recordStats"]["recordCount"]

    assert ibkr_main(["--once"]) == 0
    assert tr_main(["--once"]) == 0

    assert wallet.compute_account_view(ibkr_acc)["recordStats"]["recordCount"] == ibkr_count_before
    assert (
        wallet.compute_account_view(tr_wealth_acc)["recordStats"]["recordCount"]
        == tr_wealth_count_before
    )
    assert (
        wallet.compute_account_view(tr_cash_acc)["recordStats"]["recordCount"]
        == tr_cash_count_before
    )
    get_settings.cache_clear()
