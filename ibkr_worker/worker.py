"""Polling worker syncing IBKR settled trades, dividends, and portfolio valuation."""

from __future__ import annotations

import argparse
import json
import logging
import time
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
from shared.database import DeduplicationStore, SeenRecordEntry, compute_record_hash
from shared.mock_data import load_mock_ibkr_xml

from ibkr_worker.flex_client import FlexClient, IBKRStatementData, parse_flex_xml

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IBKRSyncSummary:
    """Summary of a single IBKR sync iteration."""

    trades_synced: int
    cash_events_synced: int
    skipped_duplicates: int
    patched_initial_balance: float | None
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
    ) -> None:
        self.settings = settings
        self.db = db
        self.bb_client = bb_client
        self._owns_flex_client = flex_client is None
        self.flex_client = flex_client or FlexClient(
            token=settings.ibkr_flex_token.get_secret_value(),
            query_id=settings.ibkr_flex_query_id,
            wait_seconds=settings.ibkr_flex_wait_seconds,
        )

    def close(self) -> None:
        """Close owned FlexClient HTTP resources."""
        if self._owns_flex_client:
            self.flex_client.close()

    def fetch_statement_data(self) -> IBKRStatementData:
        """Fetch IBKR statement from mock XML fixture or live Flex Web Service."""
        if self.settings.use_mock_data:
            logger.info("Loading synthetic IBKR Flex XML payload (USE_MOCK_DATA=true)")
            return parse_flex_xml(load_mock_ibkr_xml())
        return self.flex_client.fetch_and_parse()

    def sync_once(self) -> IBKRSyncSummary:
        """Run a single sync cycle for IBKR trades, dividends, and portfolio value."""
        statement = self.fetch_statement_data()
        records_to_create: list[CreateRecordPayload] = []
        seen_entries_to_mark: list[SeenRecordEntry] = []
        trades_synced = 0
        cash_events_synced = 0
        skipped_duplicates = 0

        for trade in statement.trades:
            amount_float = round(float(trade.net_cash), 2)
            record_hash = compute_record_hash(
                self.BROKER_NAME,
                trade.trade_id,
                trade.timestamp,
                amount_float,
            )
            if self.db.is_seen(record_hash):
                skipped_duplicates += 1
                continue

            note = format_stock_buy_note(
                symbol=trade.symbol,
                isin=trade.isin,
                shares=trade.quantity,
                price=trade.price,
                currency=trade.currency,
                side=trade.side,
            )
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
            amount_float = round(float(cash_ev.amount), 2)
            record_hash = compute_record_hash(
                self.BROKER_NAME,
                cash_ev.transaction_id,
                cash_ev.timestamp,
                amount_float,
            )
            if self.db.is_seen(record_hash):
                skipped_duplicates += 1
                continue

            records_to_create.append(
                CreateRecordPayload(
                    account_id=self.settings.bb_ibkr_account_id,
                    amount=AmountPayload(value=amount_float, currency_code=cash_ev.currency),
                    category_id=self.settings.bb_category_interest_dividends,
                    record_date=cash_ev.timestamp,
                    counter_party="Interactive Brokers",
                    note=cash_ev.description[:255],
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

        is_dry_run = self.settings.dry_run or self.bb_client.dry_run
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
        if statement.total_portfolio_value > 0:
            patched_initial = self.bb_client.update_account_target_balance(
                account_id=self.settings.bb_ibkr_account_id,
                target_balance=float(statement.total_portfolio_value),
            )

        logger.info(
            "IBKR sync complete (dry_run=%s): trades=%d, cash_events=%d, "
            "duplicates=%d, patched_initial=%s",
            is_dry_run,
            trades_synced,
            cash_events_synced,
            skipped_duplicates,
            patched_initial,
        )
        return IBKRSyncSummary(
            trades_synced=trades_synced,
            cash_events_synced=cash_events_synced,
            skipped_duplicates=skipped_duplicates,
            patched_initial_balance=patched_initial,
            dry_run=is_dry_run,
        )

    def run_forever(self) -> None:
        """Continuously poll IBKR Flex Web Service at configured interval."""
        logger.info(
            "Starting IBKR worker loop (interval=%ds)",
            self.settings.ibkr_poll_interval_seconds,
        )
        while True:
            try:
                self.sync_once()
            except Exception:
                logger.exception("Error during IBKR sync iteration")
            time.sleep(self.settings.ibkr_poll_interval_seconds)


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
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.dry_run or args.dry_run_output is not None:
        settings = settings.model_copy(update={"dry_run": True})

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    db = DeduplicationStore(settings.sqlite_db_path)
    with BudgetBakersClient(
        token=settings.budgetbakers_token.get_secret_value(),
        base_url=settings.budgetbakers_base_url,
        dry_run=settings.dry_run,
        use_mock_data=settings.use_mock_data,
    ) as bb_client:
        worker = IBKRWorker(settings=settings, db=db, bb_client=bb_client)
        try:
            if args.once or args.dry_run_output is not None:
                summary = worker.sync_once()
                if args.dry_run_output is not None:
                    report: dict[str, Any] = {
                        "broker": IBKRWorker.BROKER_NAME,
                        "dry_run": summary.dry_run,
                        "summary": {
                            "trades_synced": summary.trades_synced,
                            "cash_events_synced": summary.cash_events_synced,
                            "skipped_duplicates": summary.skipped_duplicates,
                            "patched_initial_balance": summary.patched_initial_balance,
                        },
                        "would_create_records": bb_client.dry_run_created,
                        "would_patch_accounts": bb_client.dry_run_patched_accounts,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
