"""Integration tests for TRWorker including one-time historical backfill reconciliation."""

from __future__ import annotations

import json

import httpx
import respx
from config.settings import Settings
from shared.budgetbakers import BudgetBakersClient
from shared.database import DeduplicationStore
from tr_worker.worker import TRWorker


@respx.mock
def test_tr_worker_backfill_reconciliation_and_sync(
    test_settings: Settings,
    dedup_store: DeduplicationStore,
) -> None:
    """Verify TRWorker deletes legacy -50 EUR savings plans, seeds manual interest, and syncs."""
    settings = test_settings.model_copy(update={"use_mock_data": True})
    cash_acc = settings.bb_tr_cash_account_id
    wealth_acc = settings.bb_tr_wealth_account_id

    # Mock GET /records for backfill:
    # 1st call: existing Fin. investments on TR cash -> returns 2 legacy -50 EUR records
    # 2nd call: existing Interest, dividends on TR cash -> returns 2026-11-01 100.00 EUR Interest
    respx.get("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "id": "legacy-sav-1",
                            "note": "Savings plan",
                            "amount": {"value": -50.0, "currencyCode": "EUR"},
                            "recordDate": "2026-10-16T10:00:00.000Z",
                        },
                        {
                            "id": "legacy-sav-2",
                            "note": None,
                            "amount": {"value": -50.0, "currencyCode": "EUR"},
                            "recordDate": "2026-09-16T10:00:00.000Z",
                        },
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "id": "existing-int-nov",
                            "note": "Interest",
                            "amount": {"value": 100.00, "currencyCode": "EUR"},
                            "recordDate": "2026-11-01T08:00:00.000Z",
                        }
                    ]
                },
            ),
        ]
    )

    delete_route = respx.delete("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(200, json={"summary": {"succeeded": 2}})
    )
    post_records_route = respx.post("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": f"new-{i}", "success": True} for i in range(8)]},
        )
    )
    respx.get("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "id": wealth_acc,
                        "recordStats": {
                            "totalIncomes": 25.00,
                            "totalExpenses": 50.00,
                        },
                    }
                ]
            },
        )
    )
    patch_route = respx.patch("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": wealth_acc, "success": True}]},
        )
    )

    with BudgetBakersClient(token="test-token") as bb_client:
        worker = TRWorker(settings=settings, db=dedup_store, bb_client=bb_client)

        # First sync:
        # - Deletes 2 legacy -50 EUR savings plan placeholders
        # - Seeds 1 existing manual interest record (100.00 EUR on 2026-11-01)
        # - Creates 8 new records (9 parsed minus 1 seeded interest)
        # - Patches wealth account initialBalance = 1000.00 - 25.00 + 50.00 = 1025.00
        summary1 = worker.sync_once()
        assert summary1.deleted_legacy_savings == 2
        assert summary1.records_created == 8
        assert summary1.skipped_duplicates == 1
        assert summary1.patched_wealth_initial_balance == 1025.00
        assert delete_route.call_count == 1
        assert post_records_route.call_count == 1
        assert patch_route.call_count == 1

        pushed = json.loads(post_records_route.calls[0].request.content)
        cash_pushed = [r for r in pushed if r["accountId"] == cash_acc]
        wealth_pushed = [r for r in pushed if r["accountId"] == wealth_acc]
        assert len(cash_pushed) == 5
        assert len(wealth_pushed) == 3

        # Second sync: backfill not repeated, 0 new records created
        summary2 = worker.sync_once()
        assert summary2.deleted_legacy_savings == 0
        assert summary2.records_created == 0
        assert summary2.skipped_duplicates == 9
        assert delete_route.call_count == 1
        assert post_records_route.call_count == 1


@respx.mock
def test_tr_worker_dry_run_previews_backfill_without_mutating_sqlite(
    test_settings: Settings,
    dedup_store: DeduplicationStore,
) -> None:
    """Verify TRWorker in dry_run mode previews backfill and creations without mutating SQLite."""
    dry_settings = test_settings.model_copy(update={"use_mock_data": True, "dry_run": True})
    wealth_acc = dry_settings.bb_tr_wealth_account_id

    respx.get("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "id": "legacy-sav-1",
                            "note": "Savings plan",
                            "amount": {"value": -50.0, "currencyCode": "EUR"},
                            "recordDate": "2026-10-16T10:00:00.000Z",
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "id": "existing-int-nov",
                            "note": "Interest",
                            "amount": {"value": 100.00, "currencyCode": "EUR"},
                            "recordDate": "2026-11-01T08:00:00.000Z",
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "id": "legacy-sav-1",
                            "note": "Savings plan",
                            "amount": {"value": -50.0, "currencyCode": "EUR"},
                            "recordDate": "2026-10-16T10:00:00.000Z",
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "id": "existing-int-nov",
                            "note": "Interest",
                            "amount": {"value": 100.00, "currencyCode": "EUR"},
                            "recordDate": "2026-11-01T08:00:00.000Z",
                        }
                    ]
                },
            ),
        ]
    )
    delete_route = respx.delete("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(200, json={"summary": {"succeeded": 1}})
    )
    post_records_route = respx.post("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": f"new-{i}", "success": True} for i in range(8)]},
        )
    )
    respx.get("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "id": wealth_acc,
                        "recordStats": {
                            "totalIncomes": 25.00,
                            "totalExpenses": 50.00,
                        },
                    }
                ]
            },
        )
    )
    patch_route = respx.patch("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": wealth_acc, "success": True}]},
        )
    )

    with BudgetBakersClient(token="test-token", dry_run=True, use_mock_data=False) as dry_bb:
        dry_worker = TRWorker(settings=dry_settings, db=dedup_store, bb_client=dry_bb)
        dry_summary = dry_worker.sync_once()

        assert dry_summary.dry_run is True
        assert dry_summary.deleted_legacy_savings == 1
        assert dry_summary.records_created == 8
        assert dry_summary.skipped_duplicates == 1
        assert dry_summary.patched_wealth_initial_balance == 1025.00

        assert delete_route.call_count == 0
        assert post_records_route.call_count == 0
        assert patch_route.call_count == 0
        # Neither seen_records nor sync_metadata was mutated
        assert dedup_store.count() == 0
        assert dedup_store.get_metadata("tr_backfill_reconciled_v1") is None
        assert dry_bb.dry_run_deleted_ids == ["legacy-sav-1"]
        assert len(dry_bb.dry_run_created) == 8

    # Subsequent live run executes backfill and persists to SQLite
    live_settings = test_settings.model_copy(update={"use_mock_data": True, "dry_run": False})
    with BudgetBakersClient(token="test-token", dry_run=False) as live_bb:
        live_worker = TRWorker(settings=live_settings, db=dedup_store, bb_client=live_bb)
        live_summary = live_worker.sync_once()
        assert live_summary.dry_run is False
        assert live_summary.deleted_legacy_savings == 1
        assert live_summary.records_created == 8
        assert live_summary.skipped_duplicates == 1
        assert delete_route.call_count == 1
        assert post_records_route.call_count == 1
        assert patch_route.call_count == 1
        assert dedup_store.count() == 9
        assert dedup_store.get_metadata("tr_backfill_reconciled_v1") == "true"
