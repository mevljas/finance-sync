"""Integration tests for IBKRWorker end-to-end sync cycle."""

from __future__ import annotations

import json

import httpx
import respx
from config.settings import Settings
from ibkr_worker.worker import IBKRWorker
from shared.budgetbakers import BudgetBakersClient
from shared.database import DeduplicationStore


@respx.mock
def test_ibkr_worker_sync_once_and_idempotency(
    test_settings: Settings,
    dedup_store: DeduplicationStore,
) -> None:
    """Verify IBKRWorker syncs trades & dividends, patches balance, and deduplicates on rerun."""
    settings = test_settings.model_copy(update={"use_mock_data": True})
    acc_id = settings.bb_ibkr_account_id

    post_records_route = respx.post("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "rec-1", "success": True},
                    {"id": "rec-2", "success": True},
                    {"id": "rec-3", "success": True},
                ]
            },
        )
    )
    respx.get("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "id": acc_id,
                        "recordStats": {
                            "totalIncomes": 2000.00,
                            "totalExpenses": 1500.00,
                        },
                    }
                ]
            },
        )
    )
    patch_acc_route = respx.patch("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": acc_id, "success": True}]},
        )
    )

    with BudgetBakersClient(token="test-token") as bb_client:
        worker = IBKRWorker(settings=settings, db=dedup_store, bb_client=bb_client)

        # First sync: 2 trades + 1 dividend synced
        summary1 = worker.sync_once()
        assert summary1.trades_synced == 2
        assert summary1.cash_events_synced == 1
        assert summary1.skipped_duplicates == 0
        # Target = 10100.00 -> initial = 10100.00 - 2000.00 + 1500.00 = 9600.00
        assert summary1.patched_initial_balance == 9600.00
        assert post_records_route.call_count == 1
        assert patch_acc_route.call_count == 1

        pushed_batch = json.loads(post_records_route.calls[0].request.content)
        assert len(pushed_batch) == 3
        assert "BUY 5.0000 sh XF0000000001 (FAKE) @ 100.00 EUR" in pushed_batch[0]["note"]

        # Second sync: all 3 records skipped via SQLite SHA-256 deduplication
        summary2 = worker.sync_once()
        assert summary2.trades_synced == 0
        assert summary2.cash_events_synced == 0
        assert summary2.skipped_duplicates == 3
        assert post_records_route.call_count == 1


@respx.mock
def test_ibkr_worker_dry_run_never_mutates_sqlite_or_wallet(
    test_settings: Settings,
    dedup_store: DeduplicationStore,
) -> None:
    """Verify IBKRWorker in dry_run mode previews sync and leaves SQLite and Wallet untouched."""
    dry_settings = test_settings.model_copy(update={"use_mock_data": True, "dry_run": True})
    acc_id = dry_settings.bb_ibkr_account_id

    get_acc_route = respx.get("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "id": acc_id,
                        "recordStats": {
                            "totalIncomes": 2000.00,
                            "totalExpenses": 1500.00,
                        },
                    }
                ]
            },
        )
    )
    post_records_route = respx.post("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "rec-1", "success": True},
                    {"id": "rec-2", "success": True},
                    {"id": "rec-3", "success": True},
                ]
            },
        )
    )
    patch_acc_route = respx.patch("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": acc_id, "success": True}]},
        )
    )

    with BudgetBakersClient(token="test-token", dry_run=True, use_mock_data=False) as dry_bb:
        dry_worker = IBKRWorker(settings=dry_settings, db=dedup_store, bb_client=dry_bb)
        dry_summary = dry_worker.sync_once()

        assert dry_summary.dry_run is True
        assert dry_summary.trades_synced == 2
        assert dry_summary.cash_events_synced == 1
        assert dry_summary.skipped_duplicates == 0
        assert dry_summary.patched_initial_balance == 9600.00
        assert get_acc_route.call_count == 1
        assert post_records_route.call_count == 0
        assert patch_acc_route.call_count == 0
        # SQLite seen_records must remain completely empty after dry-run
        assert dedup_store.count() == 0
        assert len(dry_bb.dry_run_created) == 3

    # Subsequent real run must still see all 3 records as new and persist them
    live_settings = test_settings.model_copy(update={"use_mock_data": True, "dry_run": False})
    with BudgetBakersClient(token="test-token", dry_run=False) as live_bb:
        live_worker = IBKRWorker(settings=live_settings, db=dedup_store, bb_client=live_bb)
        live_summary = live_worker.sync_once()
        assert live_summary.dry_run is False
        assert live_summary.trades_synced == 2
        assert live_summary.cash_events_synced == 1
        assert dedup_store.count() == 3
        assert post_records_route.call_count == 1
        assert patch_acc_route.call_count == 1
