"""Polling worker syncing IBKR settled trades, dividends, and portfolio valuation."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import Settings, get_settings
from shared.budgetbakers import (
    MAX_RECORDS_BATCH_SIZE,
    AmountPayload,
    BudgetBakersClient,
    CreateRecordPayload,
    format_stock_buy_note,
)
from shared.couchdb import (
    CouchDBStockSyncClient,
    LivePositionInput,
    StockTradeInput,
    load_delete_file,
)
from shared.database import DeduplicationStore, SeenRecordEntry, compute_record_hash
from shared.discord import attach_discord_error_handler
from shared.mock_data import load_mock_ibkr_xml
from shared.scheduler import parse_start_time, run_periodic_sync

from ibkr_worker.flex_client import (
    FlexClient,
    IBKRStatementData,
    merge_statement_data,
    parse_flex_xml,
)

logger = logging.getLogger(__name__)


def _resolve_xml_files(paths: Sequence[Path]) -> list[Path]:
    """Expand file and directory paths into a sorted list of .xml files."""
    resolved: list[Path] = []
    for path in paths:
        if path.is_dir():
            xml_in_dir = sorted(
                p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".xml"
            )
            if not xml_in_dir:
                raise FileNotFoundError(f"No .xml files found in directory: {path}")
            resolved.extend(xml_in_dir)
        elif path.is_file():
            resolved.append(path)
        else:
            raise FileNotFoundError(f"IBKR XML path not found: {path}")
    return resolved


@dataclass(frozen=True, slots=True)
class IBKRSyncSummary:
    """Summary of a single IBKR sync iteration."""

    trades_synced: int
    cash_events_synced: int
    skipped_duplicates: int
    patched_initial_balance: float | None
    deleted_existing_records: int = 0
    dry_run: bool = False


class IBKRWorker:
    """Orchestrates IBKR Flex ingestion, SQLite deduplication, and BudgetBakers push."""

    BROKER_NAME = "ibkr"

    def __init__(
        self,
        settings: Settings,
        db: DeduplicationStore,
        bb_client: BudgetBakersClient,
        flex_client: FlexClient | None = None,
        couchdb_client: CouchDBStockSyncClient | None = None,
        xml_paths: Sequence[Path] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.bb_client = bb_client
        self.xml_paths: list[Path] = list(xml_paths) if xml_paths else []
        self._owns_flex_client = flex_client is None
        self.flex_client = flex_client or FlexClient(
            token=settings.ibkr_flex_token.get_secret_value(),
            query_id=settings.ibkr_flex_query_id,
            wait_seconds=settings.ibkr_flex_wait_seconds,
        )
        self._owns_couchdb_client = couchdb_client is None and settings.has_couchdb
        if couchdb_client is not None:
            self.couchdb_client: CouchDBStockSyncClient | None = couchdb_client
        elif settings.has_couchdb:
            self.couchdb_client = CouchDBStockSyncClient(
                owner_id=settings.bb_couchdb_user,
                password=settings.bb_couchdb_password.get_secret_value(),
                base_url=settings.bb_couchdb_url,
                dry_run=settings.dry_run or bb_client.dry_run,
                use_mock_data=settings.use_mock_data,
            )
        else:
            self.couchdb_client = None

    def close(self) -> None:
        """Close owned FlexClient and CouchDB HTTP resources."""
        if self._owns_flex_client:
            self.flex_client.close()
        if self._owns_couchdb_client and self.couchdb_client is not None:
            self.couchdb_client.close()

    def fetch_statement_data(self) -> IBKRStatementData:
        """Fetch IBKR statement from local XML file(s)/dir, mock XML, or live Flex Web Service."""
        if self.xml_paths:
            xml_files = _resolve_xml_files(self.xml_paths)
            logger.info(
                "Loading %d local IBKR Flex XML file(s): %s",
                len(xml_files),
                [str(p) for p in xml_files],
            )
            parsed = [parse_flex_xml(xml_file.read_bytes()) for xml_file in xml_files]
            return merge_statement_data(parsed)
        if self.settings.use_mock_data:
            logger.info("Loading synthetic IBKR Flex XML payload (USE_MOCK_DATA=true)")
            return parse_flex_xml(load_mock_ibkr_xml())
        return self.flex_client.fetch_and_parse()

    def sync_once(
        self,
        *,
        purge_existing_records: bool = False,
        delete_file: Path | None = None,
    ) -> IBKRSyncSummary:
        """Run a single sync cycle for IBKR trades, dividends, and portfolio value."""
        is_dry_run = self.settings.dry_run or self.bb_client.dry_run
        file_record_ids, couch_delete_ids = load_delete_file(delete_file)
        deleted_existing = 0
        if purge_existing_records:
            existing_records = self.bb_client.get_records(self.settings.bb_ibkr_account_id)
            existing_ids = [str(rec["id"]) for rec in existing_records if rec.get("id") is not None]
            if existing_ids:
                deleted_existing = self.bb_client.delete_records(existing_ids)
        elif file_record_ids:
            deleted_existing = self.bb_client.delete_records(file_record_ids)

        existing_wallet_keys: set[tuple[str, float, str]] = set()
        if (
            not purge_existing_records
            and not self.settings.use_mock_data
            and self.db.count(self.BROKER_NAME) == 0
        ):
            for rec in self.bb_client.get_records(
                self.settings.bb_ibkr_account_id,
                record_date_gte="2024-01-01T00:00:00Z",
            ):
                r_date = str(rec.get("recordDate") or "")[:10]
                r_amt = round(float((rec.get("amount") or {}).get("value") or 0.0), 2)
                r_note = str(rec.get("note") or "").strip()
                if r_date:
                    existing_wallet_keys.add((r_date, r_amt, r_note))

        statement = self.fetch_statement_data()
        records_to_create: list[CreateRecordPayload] = []
        seen_entries_to_mark: list[SeenRecordEntry] = []
        seed_entries_to_mark: list[SeenRecordEntry] = []
        trades_synced = 0
        cash_events_synced = 0
        skipped_duplicates = 0

        net_couch_cash_eur = 0.0
        if self.couchdb_client is not None:
            couch_trades = [
                StockTradeInput(
                    external_id=trade.trade_id,
                    timestamp=trade.timestamp,
                    symbol=trade.symbol,
                    isin=trade.isin,
                    quantity=trade.quantity,
                    price=trade.price,
                    price_currency=trade.currency,
                    cash_amount_eur=abs(trade.net_cash) * trade.fx_rate_to_base,
                    side=trade.side,
                )
                for trade in statement.trades
            ]
            couch_positions = [
                LivePositionInput(
                    symbol=pos.symbol,
                    isin=pos.isin,
                    quantity=pos.quantity,
                    unit_price=pos.mark_price,
                    unit_price_currency=pos.currency,
                    unit_price_eur=pos.mark_price * pos.fx_rate_to_base,
                )
                for pos in statement.open_positions
            ]
            couch_result = self.couchdb_client.sync_account_holdings(
                broker=self.BROKER_NAME,
                account_id=self.settings.bb_ibkr_account_id,
                trades=couch_trades,
                live_positions=couch_positions,
                purge_existing=purge_existing_records,
                delete_doc_ids=couch_delete_ids,
                zero_cost_baseline_assets=self.settings.zero_cost_baseline_asset_set,
            )
            trades_synced = couch_result.transactions_upserted
            net_couch_cash_eur = couch_result.net_cash_invested_eur
        else:
            for trade in statement.trades:
                amount_float = round(float(trade.net_cash), 2)
                record_hash = compute_record_hash(
                    self.BROKER_NAME,
                    trade.trade_id,
                    trade.timestamp,
                    amount_float,
                )
                note = format_stock_buy_note(
                    symbol=trade.symbol,
                    isin=trade.isin,
                    shares=trade.quantity,
                    price=trade.price,
                    currency=trade.currency,
                    side=trade.side,
                )
                if self.db.is_seen(record_hash):
                    skipped_duplicates += 1
                    continue
                if (trade.timestamp[:10], amount_float, note.strip()) in existing_wallet_keys:
                    seed_entries_to_mark.append(
                        SeenRecordEntry(
                            record_hash=record_hash,
                            broker=self.BROKER_NAME,
                            timestamp=trade.timestamp,
                        )
                    )
                    skipped_duplicates += 1
                    continue

                records_to_create.append(
                    CreateRecordPayload(
                        account_id=self.settings.bb_ibkr_account_id,
                        amount=AmountPayload(value=amount_float, currency_code=trade.currency),
                        category_id=self.settings.bb_category_investments,
                        record_date=trade.timestamp,
                        counter_party="Interactive Brokers",
                        note=note,
                    )
                )
                seen_entries_to_mark.append(
                    SeenRecordEntry(
                        record_hash=record_hash,
                        broker=self.BROKER_NAME,
                        timestamp=trade.timestamp,
                    )
                )
                trades_synced += 1

        for cash_ev in statement.cash_events:
            eur_amount = round(float(cash_ev.amount * cash_ev.fx_rate_to_base), 2)
            record_hash = compute_record_hash(
                self.BROKER_NAME,
                cash_ev.transaction_id,
                cash_ev.timestamp,
                eur_amount,
            )
            desc = (
                f"{cash_ev.description} ({cash_ev.amount} {cash_ev.currency})"
                if cash_ev.currency.upper() != "EUR"
                else cash_ev.description
            )
            note_str = desc[:255]
            if self.db.is_seen(record_hash):
                skipped_duplicates += 1
                continue
            if eur_amount == 0.0:
                continue
            if (cash_ev.timestamp[:10], eur_amount, note_str.strip()) in existing_wallet_keys:
                seed_entries_to_mark.append(
                    SeenRecordEntry(
                        record_hash=record_hash,
                        broker=self.BROKER_NAME,
                        timestamp=cash_ev.timestamp,
                    )
                )
                skipped_duplicates += 1
                continue

            records_to_create.append(
                CreateRecordPayload(
                    account_id=self.settings.bb_ibkr_account_id,
                    amount=AmountPayload(value=eur_amount, currency_code="EUR"),
                    category_id=self.settings.bb_category_interest_dividends,
                    record_date=cash_ev.timestamp,
                    counter_party="Interactive Brokers",
                    note=note_str,
                )
            )
            seen_entries_to_mark.append(
                SeenRecordEntry(
                    record_hash=record_hash,
                    broker=self.BROKER_NAME,
                    timestamp=cash_ev.timestamp,
                )
            )
            cash_events_synced += 1

        if seed_entries_to_mark and not is_dry_run:
            self.db.mark_seen_batch(seed_entries_to_mark)

        for start in range(0, len(records_to_create), MAX_RECORDS_BATCH_SIZE):
            rec_batch = records_to_create[start : start + MAX_RECORDS_BATCH_SIZE]
            seen_batch = seen_entries_to_mark[start : start + MAX_RECORDS_BATCH_SIZE]
            self.bb_client.create_records(rec_batch)
            if not is_dry_run:
                self.db.mark_seen_batch(seen_batch)

        if is_dry_run and seen_entries_to_mark:
            logger.info(
                "[DRY_RUN] Skipping SQLite seen_records persistence for %d IBKR records",
                len(seen_entries_to_mark),
            )

        patched_initial: float | None = None
        if self.couchdb_client is not None:
            # Wallet UI subtracts BUY AssetTransaction cashAmount from REST currentBalance,
            # so REST target_balance must include net_couch_cash_eur to keep settled cash exact.
            patched_initial = self.bb_client.update_account_target_balance(
                account_id=self.settings.bb_ibkr_account_id,
                target_balance=round(float(statement.ending_cash) + net_couch_cash_eur, 2),
            )
        elif statement.total_portfolio_value > 0:
            patched_initial = self.bb_client.update_account_target_balance(
                account_id=self.settings.bb_ibkr_account_id,
                target_balance=float(statement.total_portfolio_value),
            )

        logger.info(
            "IBKR sync complete (dry_run=%s): trades=%d, cash_events=%d, "
            "duplicates=%d, deleted_existing=%d, patched_initial=%s",
            is_dry_run,
            trades_synced,
            cash_events_synced,
            skipped_duplicates,
            deleted_existing,
            patched_initial,
        )
        return IBKRSyncSummary(
            trades_synced=trades_synced,
            cash_events_synced=cash_events_synced,
            skipped_duplicates=skipped_duplicates,
            patched_initial_balance=patched_initial,
            deleted_existing_records=deleted_existing,
            dry_run=is_dry_run,
        )

    def run_forever(self) -> None:
        """Continuously poll IBKR Flex Web Service at configured interval."""
        run_periodic_sync(
            worker_name="IBKR",
            interval_seconds=self.settings.ibkr_poll_interval_seconds,
            sync_fn=self.sync_once,
            start_time=self.settings.sync_start_time,
            timezone_name=self.settings.sync_timezone,
        )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for ibkr_worker."""
    parser = argparse.ArgumentParser(description="IBKR to BudgetBakers sync worker")
    parser.add_argument("--once", action="store_true", help="Run a single sync cycle and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview sync without writing to BudgetBakers or SQLite",
    )
    parser.add_argument(
        "--dry-run-output",
        type=Path,
        default=None,
        help="Write JSON dry-run preview report to file (implies --dry-run)",
    )
    parser.add_argument(
        "--purge-account-records",
        action="store_true",
        help="Delete existing records on BB_IBKR_ACCOUNT_ID before syncing",
    )
    parser.add_argument(
        "--delete-file",
        type=Path,
        default=None,
        help="Path to JSON file listing specific record_ids and couchdb_doc_ids to delete",
    )
    parser.add_argument(
        "--xml-file",
        type=Path,
        nargs="+",
        default=None,
        help="Path to IBKR Flex XML file(s) or a directory of *.xml files (implies --once)",
    )
    parser.add_argument(
        "--start-time",
        type=str,
        default=None,
        help="Optional daily start time (HH:MM or HH:MM:SS) overriding SYNC_START_TIME",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    updates: dict[str, Any] = {}
    if args.dry_run or args.dry_run_output is not None:
        updates["dry_run"] = True
    if args.start_time is not None:
        updates["sync_start_time"] = parse_start_time(args.start_time)
    if updates:
        settings = settings.model_copy(update=updates)

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    with attach_discord_error_handler(settings, worker_name="IBKR"):
        try:
            db = DeduplicationStore(settings.sqlite_db_path)
            with BudgetBakersClient(
                token=settings.budgetbakers_token.get_secret_value(),
                base_url=settings.budgetbakers_base_url,
                dry_run=settings.dry_run,
                use_mock_data=settings.use_mock_data,
            ) as bb_client:
                worker = IBKRWorker(
                    settings=settings,
                    db=db,
                    bb_client=bb_client,
                    xml_paths=args.xml_file,
                )
                try:
                    if (
                        args.once
                        or args.dry_run_output is not None
                        or args.purge_account_records
                        or args.delete_file is not None
                        or args.xml_file is not None
                    ):
                        summary = worker.sync_once(
                            purge_existing_records=args.purge_account_records,
                            delete_file=args.delete_file,
                        )
                        if args.dry_run_output is not None:
                            report: dict[str, Any] = {
                                "broker": IBKRWorker.BROKER_NAME,
                                "dry_run": summary.dry_run,
                                "summary": {
                                    "trades_synced": summary.trades_synced,
                                    "cash_events_synced": summary.cash_events_synced,
                                    "skipped_duplicates": summary.skipped_duplicates,
                                    "deleted_existing_records": summary.deleted_existing_records,
                                    "patched_initial_balance": summary.patched_initial_balance,
                                },
                                "would_delete_record_ids": bb_client.dry_run_deleted_ids,
                                "would_create_records": bb_client.dry_run_created,
                                "would_patch_accounts": bb_client.dry_run_patched_accounts,
                                "would_upsert_couchdb_docs": (
                                    worker.couchdb_client.dry_run_upserted_docs
                                    if worker.couchdb_client is not None
                                    else []
                                ),
                                "would_delete_couchdb_doc_ids": (
                                    worker.couchdb_client.dry_run_deleted_ids
                                    if worker.couchdb_client is not None
                                    else []
                                ),
                            }
                            args.dry_run_output.parent.mkdir(parents=True, exist_ok=True)
                            args.dry_run_output.write_text(
                                json.dumps(report, indent=2),
                                encoding="utf-8",
                            )
                    else:
                        worker.run_forever()
                finally:
                    worker.close()
        except Exception:
            logger.exception("IBKR worker failed")
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
