"""Unit tests for shared/budgetbakers.py and shared/database.py."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from config.settings import (
    CATEGORY_BAR_CAFE,
    CATEGORY_GROCERIES,
    CATEGORY_RESTAURANTS,
    CATEGORY_SOFTWARE_GAMES,
    Settings,
)
from shared.budgetbakers import (
    AmountPayload,
    BudgetBakersAPIError,
    BudgetBakersClient,
    CreateRecordPayload,
    format_stock_buy_note,
)
from shared.database import DeduplicationStore, SeenRecordEntry, compute_record_hash


def test_compute_record_hash_matches_sha256_spec() -> None:
    """Verify hash = sha256(broker + external_id + timestamp + f'{float(amount):.2f}')."""
    expected = hashlib.sha256(b"ibkrTRD-10012026-10-15T12:00:00Z-501.00").hexdigest()
    actual_float = compute_record_hash("ibkr", "TRD-1001", "2026-10-15T12:00:00Z", -501.0)
    actual_dec = compute_record_hash(
        "ibkr", "TRD-1001", "2026-10-15T12:00:00Z", Decimal("-501.0000")
    )
    actual_str = compute_record_hash("ibkr", "TRD-1001", "2026-10-15T12:00:00Z", "-501")
    assert actual_float == expected
    assert actual_dec == expected
    assert actual_str == expected


def test_deduplication_store_crud_and_metadata(tmp_path: Path) -> None:
    """Verify SQLite WAL deduplication store operations and metadata persistence."""
    store = DeduplicationStore(tmp_path / "dedup.db")
    h1 = compute_record_hash("ibkr", "1", "2026-10-01T00:00:00Z", -100.0)
    h2 = compute_record_hash("tr", "2", "2026-10-02T00:00:00Z", 50.0)

    assert not store.is_seen(h1)
    store.mark_seen(h1, "ibkr", "2026-10-01T00:00:00Z")
    assert store.is_seen(h1)
    assert store.count() == 1
    assert store.count("ibkr") == 1
    assert store.count("tr") == 0

    inserted = store.mark_seen_batch(
        [
            SeenRecordEntry(h1, "ibkr", "2026-10-01T00:00:00Z"),
            SeenRecordEntry(h2, "tr", "2026-10-02T00:00:00Z"),
        ]
    )
    assert inserted >= 1
    assert store.get_seen_hashes() == {h1, h2}
    assert store.get_seen_hashes("tr") == {h2}
    assert store.mark_seen_batch([]) == 0

    assert store.get_metadata("backfill") is None
    store.set_metadata("backfill", "v1")
    assert store.get_metadata("backfill") == "v1"
    store.set_metadata("backfill", "v2")
    assert store.get_metadata("backfill") == "v2"


def test_format_stock_buy_note_embeds_shares_isin_and_price() -> None:
    """Verify stock buy note contains executed shares, ISIN, symbol, and price."""
    note = format_stock_buy_note(
        symbol="FAKE",
        isin="XF0000000001",
        shares=Decimal("5.0000"),
        price=Decimal("100.00"),
        currency="EUR",
    )
    assert "5.0000 sh" in note
    assert "XF0000000001" in note
    assert "FAKE" in note
    assert "100.00 EUR" in note


def test_settings_merchant_category_resolution(test_settings: Settings) -> None:
    """Verify merchant keyword and MCC category resolution."""
    assert test_settings.resolve_merchant_category("ACME SUPERMARKET") == CATEGORY_GROCERIES
    assert test_settings.resolve_merchant_category("FAKE BURGER DINER") == CATEGORY_RESTAURANTS
    assert test_settings.resolve_merchant_category("SAMPLE COFFEE SHOP") == CATEGORY_BAR_CAFE
    assert test_settings.resolve_merchant_category("Steam") == CATEGORY_SOFTWARE_GAMES
    assert test_settings.resolve_merchant_category("Random Store", mcc="5411") == CATEGORY_GROCERIES
    assert (
        test_settings.resolve_merchant_category("Unknown Merchant")
        == test_settings.bb_category_unknown_expense
    )


@respx.mock
def test_budgetbakers_create_records_batches_in_chunks_of_20() -> None:
    """Verify POST /v1/api/records splits 25 records into 2 batches (20 + 5)."""
    route = respx.post("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"results": [{"id": f"rec-{i}", "success": True} for i in range(20)]},
            ),
            httpx.Response(
                200,
                json={"results": [{"id": f"rec-{i}", "success": True} for i in range(20, 25)]},
            ),
        ]
    )

    records = [
        CreateRecordPayload(
            account_id="acc-1",
            amount=AmountPayload(value=-10.0 - i, currency_code="EUR"),
            category_id="cat-1",
            record_date="2026-11-01T12:00:00Z",
            counter_party="Merchant",
            note=f"Note {i}",
        )
        for i in range(25)
    ]

    with BudgetBakersClient(token="jwt-token") as client:
        assert client.create_records([]) == []
        created_ids = client.create_records(records)

    assert len(created_ids) == 25
    assert route.call_count == 2
    first_batch = json.loads(route.calls[0].request.content)
    second_batch = json.loads(route.calls[1].request.content)
    assert len(first_batch) == 20
    assert len(second_batch) == 5


@respx.mock
def test_budgetbakers_update_account_target_balance_formula() -> None:
    """Verify initialBalance = target_balance - totalIncomes + totalExpenses via PATCH."""
    account_id = "11111111-1111-4111-8111-111111111111"
    respx.get("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "id": account_id,
                        "name": "Demo Investment Account",
                        "recordStats": {
                            "totalIncomes": 2000.0,
                            "totalExpenses": 500.0,
                        },
                        "balance": {
                            "initial": 8500.0,
                            "currentBalance": 10000.0,
                        },
                    }
                ]
            },
        )
    )
    patch_route = respx.patch("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": account_id, "success": True}]},
        )
    )

    with BudgetBakersClient(token="jwt-token") as client:
        # Target = 10100.00 -> initial = 10100.00 - 2000.0 + 500.0 = 8600.0
        new_initial = client.update_account_target_balance(account_id, 10100.00)

    assert new_initial == 8600.0
    assert patch_route.call_count == 1
    patch_body = json.loads(patch_route.calls[0].request.content)
    assert patch_body == [{"id": account_id, "initialBalance": 8600.0}]


@respx.mock
def test_budgetbakers_delete_records_and_rate_limit_retry() -> None:
    """Verify DELETE /v1/api/records batching (max 10) and 429 Retry-After handling."""
    slept: list[float] = []
    delete_route = respx.delete("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, text="Too Many Requests"),
            httpx.Response(200, json={"summary": {"succeeded": 10}}),
            httpx.Response(200, json={"summary": {"succeeded": 2}}),
        ]
    )

    ids = [f"id-{i}" for i in range(12)]
    with BudgetBakersClient(token="jwt-token", sleep_fn=slept.append) as client:
        assert client.delete_records([]) == 0
        deleted = client.delete_records(ids)

    assert deleted == 12
    assert slept == [2.0]
    assert delete_route.call_count == 3


@respx.mock
def test_budgetbakers_dry_run_and_error_handling() -> None:
    """Verify dry_run mode performs read-only GETs, skips writes, and records dry-run previews."""
    account_id = "00000000-0000-0000-0000-000000000001"
    get_acc_route = respx.get("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "id": account_id,
                        "recordStats": {"totalIncomes": 1000.0, "totalExpenses": 250.0},
                    }
                ]
            },
        )
    )
    get_rec_route = respx.get("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(
            200,
            json={"records": [{"id": "rec-existing-1", "note": "Savings plan"}]},
        )
    )
    post_route = respx.post("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(500)
    )
    patch_route = respx.patch("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(500)
    )
    delete_route = respx.delete("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(500)
    )

    with BudgetBakersClient(token="jwt-token", dry_run=True, use_mock_data=False) as dry_client:
        res = dry_client.create_records(
            [
                CreateRecordPayload(
                    account_id=account_id,
                    amount=AmountPayload(value=-5.0),
                    category_id="cat-1",
                    record_date="2026-11-01T00:00:00Z",
                    note="Test dry-run record",
                )
            ]
        )
        assert res == ["dry-run-0"]
        assert len(dry_client.dry_run_created) == 1
        assert dry_client.dry_run_created[0]["note"] == "Test dry-run record"

        # Read-only GET /records should execute against the API when use_mock_data=False
        fetched_recs = dry_client.get_records(account_id)
        assert len(fetched_recs) == 1
        assert get_rec_route.call_count == 1

        # update_account_target_balance should call GET /accounts (1000.0 incomes, 250.0 expenses)
        # target = 5000.0 -> initial = 5000.0 - 1000.0 + 250.0 = 4250.0
        computed_initial = dry_client.update_account_target_balance(account_id, 5000.0)
        assert computed_initial == 4250.0
        assert get_acc_route.call_count == 1
        assert dry_client.dry_run_patched_accounts == [{"id": account_id, "initialBalance": 4250.0}]

        assert dry_client.delete_records(["id-1", "id-2"]) == 2
        assert dry_client.dry_run_deleted_ids == ["id-1", "id-2"]

        # Zero mutating HTTP calls made
        assert post_route.call_count == 0
        assert patch_route.call_count == 0
        assert delete_route.call_count == 0

    # Offline mock-data fallback when dry_run=True and use_mock_data=True
    respx.get("https://rest.budgetbakers.com/wallet/v1/api/accounts").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    respx.get("https://rest.budgetbakers.com/wallet/v1/api/records").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    with BudgetBakersClient(
        token="mock-budgetbakers-token", dry_run=True, use_mock_data=True
    ) as offline_client:
        stub_acc = offline_client.get_account(account_id)
        assert stub_acc["recordStats"] == {"totalIncomes": 0.0, "totalExpenses": 0.0}
        assert offline_client.get_records(account_id) == []

    with (
        BudgetBakersClient(token="bad-token") as client,
        pytest.raises(BudgetBakersAPIError) as exc_info,
    ):
        client.get_account("acc-1")
    assert exc_info.value.status_code == 401
