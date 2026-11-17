"""CouchDB client for syncing native BudgetBakers Wallet stock holdings."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_ISIN_MAP_PATHS: tuple[Path, ...] = (
    Path("/app/preview/isin_map.json"),
    Path("/app/input/isin_map.json"),
    Path("input/isin_map.json"),
)


@dataclass(frozen=True, slots=True)
class StockAssetMetadata:
    """Metadata required to match or create a -StocksFundsAsset_<uuid> document in CouchDB."""

    symbol: str
    name: str
    exchange: str
    exchange_name: str
    exchange_code: str
    currency_code: str
    price_min_date: str
    logo: str | None = None


def load_delete_file(delete_file: Path | None) -> tuple[list[str], list[str]]:
    """Load (record_ids, couchdb_doc_ids) to delete from a local JSON file."""
    if delete_file is None:
        return [], []
    if not delete_file.is_file():
        raise FileNotFoundError(f"Delete file not found: {delete_file}")
    raw: dict[str, Any] = json.loads(delete_file.read_text(encoding="utf-8"))
    record_ids = [str(x).strip() for x in raw.get("record_ids", []) if str(x).strip()]
    couchdb_ids = [str(x).strip() for x in raw.get("couchdb_doc_ids", []) if str(x).strip()]
    return record_ids, couchdb_ids


@dataclass(frozen=True, slots=True)
class StockTradeInput:
    """Normalized stock buy/sell/Saveback transaction to sync into CouchDB."""

    external_id: str
    timestamp: str
    symbol: str
    isin: str
    quantity: Decimal
    price: Decimal
    price_currency: str
    cash_amount_eur: Decimal
    side: str = "BUY"


@dataclass(frozen=True, slots=True)
class LivePositionInput:
    """Live broker position used to reconcile opening baseline shares in CouchDB."""

    symbol: str
    isin: str
    quantity: Decimal
    unit_price: Decimal
    unit_price_currency: str
    unit_price_eur: Decimal


@dataclass(frozen=True, slots=True)
class CouchDBSyncResult:
    """Summary of CouchDB stock asset and transaction synchronization."""

    assets_created: int
    transactions_upserted: int
    transactions_deleted: int
    net_cash_invested_eur: float = 0.0


def _normalize_couch_iso(ts: str) -> str:
    """Normalize an ISO timestamp to YYYY-MM-DDTHH:MM:SS.mmmZ for CouchDB."""
    cleaned = ts.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except ValueError:
        return ts


class CouchDBStockSyncClient:
    """Client for BudgetBakers Wallet CouchDB (-StocksFundsAsset & -AssetTransaction)."""

    def __init__(
        self,
        owner_id: str,
        password: str,
        base_url: str = "https://couch-prod-eu-2.budgetbakers.com",
        *,
        dry_run: bool = False,
        use_mock_data: bool = False,
        isin_map_path: Path | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.owner_id = owner_id.strip()
        self.db_url = f"{base_url.rstrip('/')}/bb-{self.owner_id}"
        self.dry_run = dry_run
        self.use_mock_data = use_mock_data
        self._owns_client = http_client is None
        self.dry_run_upserted_docs: list[dict[str, Any]] = []
        self.dry_run_deleted_ids: list[str] = []
        self._isin_map: dict[str, dict[str, Any]] = self._load_isin_map(isin_map_path)
        self._client = http_client or httpx.Client(
            timeout=20.0,
            auth=(self.owner_id, password),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    @staticmethod
    def _load_isin_map(isin_map_path: Path | None) -> dict[str, dict[str, Any]]:
        candidates = (isin_map_path,) if isin_map_path is not None else _DEFAULT_ISIN_MAP_PATHS
        for cand in candidates:
            if cand is not None and cand.is_file():
                raw: dict[str, Any] = json.loads(cand.read_text(encoding="utf-8"))
                return {
                    str(k).strip().upper(): dict(v) for k, v in raw.items() if isinstance(v, dict)
                }
        return {}

    def close(self) -> None:
        """Close the underlying HTTP client if owned."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> CouchDBStockSyncClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _fetch_docs_by_prefix(self, prefix: str) -> list[dict[str, Any]]:
        if self.use_mock_data:
            return []
        resp = self._client.get(
            f"{self.db_url}/_all_docs",
            params={
                "startkey": f'"{prefix}"',
                "endkey": f'"{prefix}\\ufff0"',
                "include_docs": "true",
            },
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        docs: list[dict[str, Any]] = []
        for row in data.get("rows", []):
            if isinstance(row, dict) and isinstance(row.get("doc"), dict):
                docs.append(row["doc"])
        return docs

    def _resolve_asset_metadata(
        self,
        isin: str,
        symbol: str,
        currency: str,
        broker: str,
    ) -> StockAssetMetadata:
        clean_isin = isin.strip().upper()
        if clean_isin in self._isin_map:
            entry = self._isin_map[clean_isin]
            use_tr_exch = broker == "tr" and "tr_exchange_code" in entry
            return StockAssetMetadata(
                symbol=str(entry.get("symbol") or symbol or clean_isin),
                name=str(entry.get("name") or symbol or clean_isin),
                exchange=str(entry.get("tr_exchange" if use_tr_exch else "exchange") or "XETR"),
                exchange_name=str(
                    entry.get("tr_exchange_name" if use_tr_exch else "exchange_name") or "FRANKFURT"
                ),
                exchange_code=str(
                    entry.get("tr_exchange_code" if use_tr_exch else "exchange_code") or "XETR"
                ),
                currency_code=str(entry.get("currency_code") or currency or "EUR"),
                price_min_date=str(
                    entry.get("tr_price_min_date" if use_tr_exch else "price_min_date")
                    or "2020-01-01"
                ),
                logo=str(entry["logo"]) if entry.get("logo") else None,
            )

        clean_sym = symbol.strip().upper() or clean_isin
        is_usd = currency.strip().upper() == "USD"
        return StockAssetMetadata(
            symbol=clean_sym[:12],
            name=symbol.strip() or clean_isin,
            exchange="NASDAQ" if is_usd else "XETR",
            exchange_name="ALL U.S. MARKET TIERS" if is_usd else "FRANKFURT",
            exchange_code="XNGS" if is_usd else "XETR",
            currency_code="USD" if is_usd else "EUR",
            price_min_date="2020-01-01",
        )

    def sync_account_holdings(
        self,
        *,
        broker: str,
        account_id: str,
        trades: Sequence[StockTradeInput],
        live_positions: Sequence[LivePositionInput],
        purge_existing: bool = False,
        delete_doc_ids: Sequence[str] | None = None,
    ) -> CouchDBSyncResult:
        """Reconcile -StocksFundsAsset and -AssetTransaction documents for an investment account."""
        now_iso = _normalize_couch_iso(datetime.now(tz=UTC).isoformat())
        couch_account_id = f"-Account_{account_id}"

        existing_assets = self._fetch_docs_by_prefix("-StocksFundsAsset_")
        existing_asset_by_id: dict[str, dict[str, Any]] = {
            str(doc["_id"]): doc for doc in existing_assets if doc.get("_id")
        }
        explicit_delete_set = set(delete_doc_ids or ())
        all_txs = self._fetch_docs_by_prefix("-AssetTransaction_")
        existing_txs = [
            doc
            for doc in all_txs
            if doc.get("accountId") == couch_account_id and int(doc.get("assetType", 0)) == 0
        ]
        existing_tx_by_id: dict[str, dict[str, Any]] = {
            str(doc["_id"]): doc for doc in existing_txs if doc.get("_id")
        }

        # Map (symbol, exchangeCode), symbol, and financeSyncIsin -> asset_id
        asset_by_sym_exch: dict[tuple[str, str], str] = {}
        asset_by_sym: dict[str, str] = {}
        asset_currency_by_id: dict[str, str] = {}
        asset_by_isin: dict[str, str] = {}
        for doc in existing_assets:
            doc_id = str(doc.get("_id") or "")
            if not doc_id or doc_id in explicit_delete_set:
                continue
            sym = str(doc.get("symbol") or "").upper()
            exch = str(doc.get("exchangeCode") or doc.get("exchange") or "").upper()
            ccy = str(doc.get("currencyCode") or "EUR").upper()
            doc_isin = str(doc.get("financeSyncIsin") or doc.get("isin") or "").strip().upper()
            asset_currency_by_id[doc_id] = ccy
            if doc_isin:
                asset_by_isin.setdefault(doc_isin, doc_id)
            if sym:
                asset_by_sym_exch[(sym, exch)] = doc_id
                asset_by_sym.setdefault(sym, doc_id)
        for doc in all_txs:
            tx_isin = str(doc.get("financeSyncIsin") or "").strip().upper()
            aid = str(doc.get("assetId") or "")
            if tx_isin and aid in asset_currency_by_id:
                asset_by_isin.setdefault(tx_isin, aid)

        docs_to_write: list[dict[str, Any]] = []
        assets_created = 0

        def ensure_asset_id(isin: str, symbol: str, currency: str) -> tuple[str, str]:
            nonlocal assets_created
            clean_isin = isin.strip().upper()
            if clean_isin not in self._isin_map and clean_isin in asset_by_isin:
                matched_id = asset_by_isin[clean_isin]
                return matched_id, asset_currency_by_id.get(matched_id, currency or "EUR")

            meta = self._resolve_asset_metadata(isin, symbol, currency, broker)
            key = (meta.symbol.upper(), meta.exchange_code.upper())
            if key in asset_by_sym_exch:
                return asset_by_sym_exch[key], meta.currency_code
            if meta.symbol.upper() in asset_by_sym:
                return asset_by_sym[meta.symbol.upper()], meta.currency_code

            det_uuid = uuid5(
                NAMESPACE_URL,
                f"finance-sync:asset:{meta.exchange_code}:{meta.symbol}",
            )
            new_asset_id = f"-StocksFundsAsset_{det_uuid}"
            asset_doc: dict[str, Any] = {
                "_id": new_asset_id,
                "symbol": meta.symbol,
                "name": meta.name,
                "exchange": meta.exchange,
                "exchangeName": meta.exchange_name,
                "exchangeCode": meta.exchange_code,
                "currencyCode": meta.currency_code,
                "priceMinDate": meta.price_min_date,
                "reservedOwnerId": self.owner_id,
                "reservedAuthorId": self.owner_id,
                "reservedSource": "android",
                "reservedCreatedAt": now_iso,
                "reservedUpdatedAt": now_iso,
                "reservedModelType": "StocksFundsAsset",
            }
            if clean_isin and clean_isin != "UNKNOWN":
                asset_doc["financeSyncIsin"] = clean_isin
                asset_by_isin[clean_isin] = new_asset_id
            if meta.logo:
                asset_doc["logo"] = meta.logo
            docs_to_write.append(asset_doc)
            asset_currency_by_id[new_asset_id] = meta.currency_code
            asset_by_sym_exch[key] = new_asset_id
            asset_by_sym[meta.symbol.upper()] = new_asset_id
            assets_created += 1
            return new_asset_id, meta.currency_code

        desired_tx_docs: dict[str, dict[str, Any]] = {}

        # Preserve any previously synced finance-sync:tx:* docs on this account
        for doc_id, doc in existing_tx_by_id.items():
            if doc.get("reservedSource") == "finance-sync-tx":
                desired_tx_docs[doc_id] = doc

        # 1. Build -AssetTransaction docs for all trades in the current sync batch
        for trade in trades:
            asset_id, asset_currency = ensure_asset_id(
                trade.isin,
                trade.symbol,
                trade.price_currency,
            )
            tx_uuid = uuid5(NAMESPACE_URL, f"finance-sync:tx:{broker}:{trade.external_id}")
            tx_doc_id = f"-AssetTransaction_{tx_uuid}"
            tx_type = 1 if trade.side.upper() == "SELL" else 0
            qty_float = round(float(abs(trade.quantity)), 6)
            price_cents = max(1, round(float(abs(trade.price)) * 100))
            cash_cents = max(1, round(float(abs(trade.cash_amount_eur)) * 100))
            norm_date = _normalize_couch_iso(trade.timestamp)

            prev_doc = existing_tx_by_id.get(tx_doc_id)
            created_at = str(prev_doc.get("reservedCreatedAt") or now_iso) if prev_doc else now_iso
            tx_doc: dict[str, Any] = {
                "_id": tx_doc_id,
                "date": norm_date,
                "quantity": qty_float,
                "cashAmount": cash_cents,
                "cashAmountCurrencyCode": "EUR",
                "price": price_cents,
                "priceCurrencyCode": asset_currency,
                "assetType": 0,
                "transactionType": tx_type,
                "accountId": couch_account_id,
                "assetId": asset_id,
                "financeSyncIsin": trade.isin.strip().upper(),
                "reservedOwnerId": self.owner_id,
                "reservedAuthorId": self.owner_id,
                "reservedSource": "finance-sync-tx",
                "reservedCreatedAt": created_at,
                "reservedUpdatedAt": now_iso,
                "reservedModelType": "AssetTransaction",
            }
            if prev_doc and "_rev" in prev_doc:
                tx_doc["_rev"] = prev_doc["_rev"]
            desired_tx_docs[tx_doc_id] = tx_doc

        # 2. Reconcile baseline pre-window shares per live position so total shares match broker
        for pos in live_positions:
            asset_id, asset_currency = ensure_asset_id(
                pos.isin,
                pos.symbol,
                pos.unit_price_currency,
            )
            synced_shares = Decimal("0")
            for doc in desired_tx_docs.values():
                if doc.get("assetId") == asset_id:
                    q = Decimal(str(doc.get("quantity") or "0"))
                    if int(doc.get("transactionType", 0)) == 1:
                        synced_shares -= q
                    else:
                        synced_shares += q

            baseline_shares = (pos.quantity - synced_shares).quantize(Decimal("0.000001"))
            if baseline_shares > Decimal("0.00001"):
                base_uuid = uuid5(
                    NAMESPACE_URL,
                    f"finance-sync:baseline:{account_id}:{asset_id}",
                )
                base_doc_id = f"-AssetTransaction_{base_uuid}"
                prev_base = existing_tx_by_id.get(base_doc_id)
                created_at = (
                    str(prev_base.get("reservedCreatedAt") or now_iso) if prev_base else now_iso
                )
                qty_f = round(float(baseline_shares), 6)
                price_cents = max(1, round(float(abs(pos.unit_price)) * 100))
                cash_cents = max(
                    1,
                    round(float(baseline_shares * abs(pos.unit_price_eur)) * 100),
                )
                base_doc: dict[str, Any] = {
                    "_id": base_doc_id,
                    "date": "2025-01-01T00:00:00.000Z",
                    "quantity": qty_f,
                    "cashAmount": cash_cents,
                    "cashAmountCurrencyCode": "EUR",
                    "price": price_cents,
                    "priceCurrencyCode": asset_currency,
                    "assetType": 0,
                    "transactionType": 0,
                    "accountId": couch_account_id,
                    "assetId": asset_id,
                    "financeSyncIsin": pos.isin.strip().upper(),
                    "reservedOwnerId": self.owner_id,
                    "reservedAuthorId": self.owner_id,
                    "reservedSource": "finance-sync-baseline",
                    "reservedCreatedAt": created_at,
                    "reservedUpdatedAt": now_iso,
                    "reservedModelType": "AssetTransaction",
                }
                if prev_base and "_rev" in prev_base:
                    base_doc["_rev"] = prev_base["_rev"]
                desired_tx_docs[base_doc_id] = base_doc

        # 3. Determine which desired docs actually changed or are new
        transactions_upserted = 0
        for doc_id, desired in desired_tx_docs.items():
            existing = existing_tx_by_id.get(doc_id)
            if existing is not None and all(
                existing.get(k) == desired.get(k)
                for k in (
                    "quantity",
                    "cashAmount",
                    "price",
                    "date",
                    "assetId",
                    "accountId",
                    "transactionType",
                    "financeSyncIsin",
                )
            ):
                continue
            docs_to_write.append(desired)
            transactions_upserted += 1

        # 4. Delete superseded -AssetTransaction (or requested -StocksFundsAsset) docs
        transactions_deleted = 0
        for doc_id, existing in existing_tx_by_id.items():
            if doc_id in desired_tx_docs:
                continue
            is_stale_baseline = existing.get("reservedSource") == "finance-sync-baseline"
            if purge_existing or doc_id in explicit_delete_set or is_stale_baseline:
                del_doc = {
                    "_id": doc_id,
                    "_rev": existing["_rev"],
                    "_deleted": True,
                }
                docs_to_write.append(del_doc)
                self.dry_run_deleted_ids.append(doc_id)
                transactions_deleted += 1
        for asset_del_id in explicit_delete_set:
            if asset_del_id in existing_asset_by_id:
                asset_existing = existing_asset_by_id[asset_del_id]
                docs_to_write.append(
                    {
                        "_id": asset_del_id,
                        "_rev": asset_existing["_rev"],
                        "_deleted": True,
                    }
                )
                self.dry_run_deleted_ids.append(asset_del_id)

        net_cash_cents = 0
        for doc in desired_tx_docs.values():
            cents = int(doc.get("cashAmount") or 0)
            if int(doc.get("transactionType", 0)) == 1:
                net_cash_cents -= cents
            else:
                net_cash_cents += cents
        net_cash_invested_eur = round(net_cash_cents / 100.0, 2)

        if not docs_to_write:
            return CouchDBSyncResult(0, 0, 0, net_cash_invested_eur=net_cash_invested_eur)

        if self.dry_run:
            for d in docs_to_write:
                if not d.get("_deleted"):
                    self.dry_run_upserted_docs.append(d)
            logger.info(
                "[DRY_RUN] CouchDB account=%s: would create %d assets, upsert %d "
                "AssetTransactions, delete %d superseded AssetTransactions (net_cash_eur=%.2f)",
                account_id,
                assets_created,
                transactions_upserted,
                transactions_deleted,
                net_cash_invested_eur,
            )
            return CouchDBSyncResult(
                assets_created=assets_created,
                transactions_upserted=transactions_upserted,
                transactions_deleted=transactions_deleted,
                net_cash_invested_eur=net_cash_invested_eur,
            )

        resp = self._client.post(
            f"{self.db_url}/_bulk_docs",
            json={"docs": docs_to_write},
        )
        resp.raise_for_status()
        logger.info(
            "CouchDB sync complete for account=%s: assets_created=%d, "
            "tx_upserted=%d, tx_deleted=%d (net_cash_eur=%.2f)",
            account_id,
            assets_created,
            transactions_upserted,
            transactions_deleted,
            net_cash_invested_eur,
        )
        return CouchDBSyncResult(
            assets_created=assets_created,
            transactions_upserted=transactions_upserted,
            transactions_deleted=transactions_deleted,
            net_cash_invested_eur=net_cash_invested_eur,
        )
