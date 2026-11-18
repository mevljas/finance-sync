"""Integration tests for TRWorker including one-time historical backfill reconciliation."""

from __future__ import annotations

import json
from datetime import time
from pathlib import Path

import httpx
import pytest
import respx
from config.settings import Settings
from shared.budgetbakers import BudgetBakersClient
from shared.database import DeduplicationStore
from tr_worker.worker import TRWorker
from tr_worker.worker import main as tr_main


@respx.mock
def test_tr_worker_backfill_reconciliation_and_sync(
    test_settings: Settings,
    dedup_store: DeduplicationStore,
    tmp_path: Path,
) -> None:
    """Verify TRWorker deletes IDs from delete_file, seeds manual interest, and syncs."""
    settings = test_settings.model_copy(update={"use_mock_data": True})
    cash_acc = settings.bb_tr_cash_account_id
    wealth_acc = settings.bb_tr_wealth_account_id

    delete_file = tmp_path / "delete_ids.json"
    delete_file.write_text(
        json.dumps({"record_ids": ["legacy-sav-1", "legacy-sav-2"]}),
        encoding="utf-8",
    )

    respx.get("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(
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
        )
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

        summary1 = worker.sync_once(delete_file=delete_file)
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
    tmp_path: Path,
) -> None:
    """Verify TRWorker in dry_run mode previews backfill and creations without mutating SQLite."""
    dry_settings = test_settings.model_copy(update={"use_mock_data": True, "dry_run": True})
    wealth_acc = dry_settings.bb_tr_wealth_account_id

    delete_file = tmp_path / "delete_ids.json"
    delete_file.write_text(
        json.dumps({"record_ids": ["legacy-sav-1"]}),
        encoding="utf-8",
    )

    respx.get("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(
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
        )
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
        dry_summary = dry_worker.sync_once(delete_file=delete_file)

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
        live_summary = live_worker.sync_once(delete_file=delete_file)
        assert live_summary.dry_run is False
        assert live_summary.deleted_legacy_savings == 1
        assert live_summary.records_created == 8
        assert live_summary.skipped_duplicates == 1
        assert delete_route.call_count == 1
        assert post_records_route.call_count == 1
        assert patch_route.call_count == 1
        assert dedup_store.count() == 9
        assert dedup_store.get_metadata("tr_backfill_reconciled_v1") == "true"


def test_tr_worker_run_forever_and_cli_start_time(
    test_settings: Settings,
    dedup_store: DeduplicationStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify TRWorker.run_forever and CLI --start-time pass start_time to scheduler."""
    captured: dict[str, object] = {}

    def fake_run_periodic_sync(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("tr_worker.worker.run_periodic_sync", fake_run_periodic_sync)

    scheduled_settings = test_settings.model_copy(
        update={
            "use_mock_data": True,
            "sync_start_time": time(7, 30),
            "sync_timezone": "UTC",
        }
    )
    with BudgetBakersClient(token="test-token", use_mock_data=True) as bb_client:
        worker = TRWorker(settings=scheduled_settings, db=dedup_store, bb_client=bb_client)
        worker.run_forever()

    assert captured["worker_name"] == "Trade Republic"
    assert captured["interval_seconds"] == scheduled_settings.tr_poll_interval_seconds
    assert captured["start_time"] == time(7, 30)
    assert captured["timezone_name"] == "UTC"

    captured.clear()
    monkeypatch.setenv("USE_MOCK_DATA", "true")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "cli_tr.db"))
    monkeypatch.setenv("TR_SESSION_DIR", str(tmp_path / "tr_session"))
    monkeypatch.setenv("SYNC_TIMEZONE", "UTC")
    assert tr_main(["--start-time", "10:20:30"]) == 0
    assert captured["start_time"] == time(10, 20, 30)
    assert captured["timezone_name"] == "UTC"


@respx.mock
def test_tr_main_notifies_discord_on_sync_error_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify tr_worker main() sends a Discord alert on --once failure when configured."""
    channel_id = "000000000000000001"
    discord_route = respx.post(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(200, json={"id": "err-msg-2"})
    )

    monkeypatch.setenv("USE_MOCK_DATA", "false")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "err_tr.db"))
    monkeypatch.setenv("TR_SESSION_DIR", str(tmp_path / "tr_sess"))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "real-bot-token")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", channel_id)
    monkeypatch.setattr(
        "tr_worker.worker.TRWorker.sync_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("TR WebSocket timeout")),
    )

    with pytest.raises(RuntimeError, match="TR WebSocket timeout"):
        tr_main(["--once"])

    assert discord_route.call_count == 1
    body = json.loads(discord_route.calls[0].request.content)
    assert "Trade Republic" in body["content"]
    assert "TR WebSocket timeout" in body["content"]
