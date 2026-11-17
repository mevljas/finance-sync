"""Unit tests for CouchDB native stock holdings sync and paired Transfer records."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from config.settings import Settings
from ibkr_worker.worker import IBKRWorker
from pydantic import SecretStr
from shared.budgetbakers import (
    AmountPayload,
    BudgetBakersClient,
    CreateRecordPayload,
    TransferPayload,
)
from shared.couchdb import (
    CouchDBStockSyncClient,
    LivePositionInput,
    StockTradeInput,
)
from shared.database import DeduplicationStore
from tr_worker.event_parser import (
    TRRecordKind,
    parse_tr_live_positions,
    parse_tr_stock_trades,
    parse_tr_timeline_events,
)
from tr_worker.worker import TRWorker


def test_create_record_payload_with_transfer_omits_category_id() -> None:
    payload = CreateRecordPayload(
        account_id="00000000-0000-0000-0000-000000000002",
        amount=AmountPayload(value=-50.0, currency_code="EUR"),
        category_id="00000000-0000-0000-0000-000000000010",
        record_date="2026-11-02T14:52:47.349Z",
        counter_party="Trade Republic",
        note="Savings plan: S&P 500 Information Tech USD (Acc)",
        transfer=TransferPayload(
            pairing_mode="new",
            account_id="00000000-0000-0000-0000-000000000003",
        ),
    )
    api_dict = payload.to_api_dict()
    assert "categoryId" not in api_dict
    assert api_dict["transfer"] == {
        "pairingMode": "new",
        "accountId": "00000000-0000-0000-0000-000000000003",
    }


def test_couchdb_sync_account_holdings_upserts_and_deletes_superseded() -> None:
    account_id = "00000000-0000-0000-0000-000000000001"
    bulk_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "_all_docs" in str(request.url):
            startkey = request.url.params.get("startkey", "")
            if "-StocksFundsAsset_" in startkey:
                return httpx.Response(
                    200,
                    json={
                        "rows": [
                            {
                                "doc": {
                                    "_id": "-StocksFundsAsset_00000000-0000-0000-0000-000000000099",
                                    "symbol": "FAKE",
                                    "exchangeCode": "XETR",
                                    "currencyCode": "EUR",
                                }
                            }
                        ]
                    },
                )
            if "-AssetTransaction_" in startkey:
                return httpx.Response(
                    200,
                    json={
                        "rows": [
                            {
                                "doc": {
                                    "_id": "-AssetTransaction_00000000-0000-0000-0000-000000000088",
                                    "_rev": "1-00000000",
                                    "accountId": f"-Account_{account_id}",
                                    "assetId": "-StocksFundsAsset_00000000",
                                    "assetType": 0,
                                    "quantity": 1,
                                    "reservedSource": "web",
                                }
                            }
                        ]
                    },
                )
        if request.method == "POST" and "_bulk_docs" in str(request.url):
            bulk_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(201, json=[{"ok": True}])
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with CouchDBStockSyncClient(
        owner_id="00000000-0000-0000-0000-000000000055",
        password="test-password",
        http_client=http_client,
    ) as client:
        result = client.sync_account_holdings(
            broker="ibkr",
            account_id=account_id,
            trades=[
                StockTradeInput(
                    external_id="T1001",
                    timestamp="2026-03-10T14:30:00Z",
                    symbol="FAKE",
                    isin="XF0000000001",
                    quantity=Decimal("10"),
                    price=Decimal("125.40"),
                    price_currency="EUR",
                    cash_amount_eur=Decimal("1255.25"),
                    side="BUY",
                ),
                StockTradeInput(
                    external_id="T1002",
                    timestamp="2026-03-11T14:30:00Z",
                    symbol="FAKEUSD",
                    isin="US0000000001",
                    quantity=Decimal("5"),
                    price=Decimal("173.45"),
                    price_currency="USD",
                    cash_amount_eur=Decimal("799.21"),
                    side="BUY",
                ),
            ],
            live_positions=[
                LivePositionInput(
                    symbol="FAKE",
                    isin="XF0000000001",
                    quantity=Decimal("160.3547"),
                    unit_price=Decimal("146.50"),
                    unit_price_currency="EUR",
                    unit_price_eur=Decimal("146.50"),
                ),
                LivePositionInput(
                    symbol="FAKEUSD",
                    isin="US0000000001",
                    quantity=Decimal("5"),
                    unit_price=Decimal("305.72"),
                    unit_price_currency="USD",
                    unit_price_eur=Decimal("265.17"),
                ),
            ],
            purge_existing=True,
        )

    http_client.close()
    assert result.assets_created == 1  # GOOG created, VWCE matched existing
    assert result.transactions_upserted == 3  # 2 trades + 1 VWCE baseline (150.3547 sh)
    assert result.transactions_deleted == 1  # old manual web transaction deleted
    assert result.net_cash_invested_eur > 0
    assert len(bulk_payloads) == 1
    docs = bulk_payloads[0]["docs"]
    deleted_docs = [d for d in docs if d.get("_deleted")]
    assert len(deleted_docs) == 1
    assert deleted_docs[0]["_id"] == "-AssetTransaction_00000000-0000-0000-0000-000000000088"
    baseline_docs = [d for d in docs if d.get("reservedSource") == "finance-sync-baseline"]
    assert len(baseline_docs) == 1
    assert baseline_docs[0]["quantity"] == 150.3547


def test_tr_event_parser_with_couchdb_creates_paired_transfer(test_settings: Settings) -> None:
    settings_with_couch = test_settings.model_copy(
        update={
            "bb_couchdb_user": "00000000-0000-0000-0000-000000000055",
            "bb_couchdb_password": SecretStr("test-password"),
        }
    )
    events = [
        {
            "id": "tr-saveback-001",
            "timestamp": "2026-03-02T08:00:00Z",
            "eventType": "saveback_execution",
            "title": "Saveback",
            "subtitle": "1% Saveback bonus",
            "status": "EXECUTED",
            "amount": {"value": 15.0, "currency": "EUR"},
            "details": {
                "isin": "IE00BK5BQT80",
                "symbol": "VWCE",
                "shares": "0.1196",
                "price": "125.42",
            },
        },
        {
            "id": "tr-sparplan-001",
            "timestamp": "2026-03-02T09:15:00Z",
            "eventType": "savings_plan_execution",
            "title": "Core S&P 500 USD (Acc)",
            "subtitle": "Savings plan executed",
            "status": "EXECUTED",
            "amount": {"value": -50.0, "currency": "EUR"},
            "details": {
                "isin": "IE00B5BMR087",
                "symbol": "CSPX",
                "shares": "0.0925",
                "price": "540.54",
            },
        },
    ]

    parsed = parse_tr_timeline_events(events, settings_with_couch)
    assert len(parsed) == 1
    rec = parsed[0]
    assert rec.kind == TRRecordKind.SAVINGS_PLAN_CASH
    api_dict = rec.payload.to_api_dict()
    assert "categoryId" not in api_dict
    assert api_dict["transfer"] == {
        "pairingMode": "new",
        "accountId": settings_with_couch.bb_tr_wealth_account_id,
    }

    stock_trades = parse_tr_stock_trades(events)
    assert len(stock_trades) == 2
    assert stock_trades[0].external_id == "tr-saveback-001:saveback"
    assert stock_trades[1].external_id == "tr-sparplan-001"


def test_ibkr_and_tr_workers_with_couchdb_dry_run(
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    settings_with_couch = test_settings.model_copy(
        update={
            "use_mock_data": True,
            "dry_run": True,
            "bb_couchdb_user": "00000000-0000-0000-0000-000000000055",
            "bb_couchdb_password": SecretStr("test-password"),
        }
    )
    db = DeduplicationStore(tmp_path / "couch_test.db")
    with BudgetBakersClient(
        token="test-token",
        dry_run=True,
        use_mock_data=True,
    ) as bb_client:
        ibkr_worker = IBKRWorker(settings=settings_with_couch, db=db, bb_client=bb_client)
        try:
            ibkr_summary = ibkr_worker.sync_once()
            assert ibkr_summary.trades_synced > 0
            assert ibkr_summary.cash_events_synced == 1
            assert ibkr_worker.couchdb_client is not None
            assert len(ibkr_worker.couchdb_client.dry_run_upserted_docs) > 0
        finally:
            ibkr_worker.close()

        tr_worker = TRWorker(settings=settings_with_couch, db=db, bb_client=bb_client)
        try:
            tr_summary = tr_worker.sync_once()
            assert tr_summary.records_created > 0
            assert tr_worker.couchdb_client is not None
            assert len(tr_worker.couchdb_client.dry_run_upserted_docs) > 0
        finally:
            tr_worker.close()


def test_parse_tr_live_positions_extracts_positions() -> None:
    positions = parse_tr_live_positions(
        {
            "positions": [
                {
                    "instrumentId": "IE00B3WJKG14",
                    "name": "S&P 500 Information Tech USD (Acc)",
                    "netSize": "38.732664",
                    "currentPrice": "34.18",
                }
            ]
        }
    )
    assert len(positions) == 1
    assert positions[0].isin == "IE00B3WJKG14"
    assert positions[0].quantity == Decimal("38.732664")
