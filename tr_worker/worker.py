"""Trade Republic event listener/poller and one-time historical backfill worker."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from config.settings import Settings, get_settings
from shared.budgetbakers import (
    MAX_RECORDS_BATCH_SIZE,
    BudgetBakersClient,
    CreateRecordPayload,
)
from shared.couchdb import CouchDBStockSyncClient, load_delete_file
from shared.database import DeduplicationStore, SeenRecordEntry, compute_record_hash
from shared.discord import attach_discord_error_handler
from shared.scheduler import parse_start_time, run_periodic_sync

from tr_worker.event_parser import (
    ParsedTRRecord,
    parse_tr_live_positions,
    parse_tr_portfolio_value,
    parse_tr_stock_trades,
    parse_tr_timeline_events,
)
from tr_worker.session_manager import Discord2FAClient, TRSessionManager

logger = logging.getLogger(__name__)

BACKFILL_META_KEY = "tr_backfill_reconciled_v1"


@dataclass(frozen=True, slots=True)
class TRSyncSummary:
    """Summary of a single Trade Republic sync iteration."""

    records_created: int
    skipped_duplicates: int
    deleted_legacy_savings: int
    patched_wealth_initial_balance: float | None
    dry_run: bool = False


def _parse_iso(ts: str) -> datetime:
    cleaned = ts.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    dt = datetime.fromisoformat(cleaned)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class TRWorker:
    """Orchestrates Trade Republic timeline polling, backfill, deduplication, and Wallet sync."""

    BROKER_NAME = "tr"

    def __init__(
        self,
        settings: Settings,
        db: DeduplicationStore,
        bb_client: BudgetBakersClient,
        session_manager: TRSessionManager | None = None,
        couchdb_client: CouchDBStockSyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.bb_client = bb_client
        self._owns_session_manager = session_manager is None
        if session_manager is not None:
            self.session_manager = session_manager
        else:
            discord_client = Discord2FAClient(
                bot_token=settings.discord_bot_token.get_secret_value(),
                channel_id=settings.discord_channel_id,
                timeout_seconds=settings.discord_2fa_timeout_seconds,
                poll_interval_seconds=settings.discord_2fa_poll_interval_seconds,
            )
            self.session_manager = TRSessionManager(
                phone_no=settings.tr_phone_no,
                pin=settings.tr_pin.get_secret_value(),
                session_dir=settings.tr_session_dir,
                discord_client=discord_client,
                use_mock_data=settings.use_mock_data,
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
        """Close owned Discord 2FA and CouchDB HTTP client resources."""
        if self._owns_session_manager:
            self.session_manager.discord_client.close()
        if self._owns_couchdb_client and self.couchdb_client is not None:
            self.couchdb_client.close()

    def _reconcile_backfill(
        self,
        parsed_records: list[ParsedTRRecord],
        *,
        is_dry_run: bool = False,
        purge_existing_records: bool = False,
        file_record_ids: list[str] | None = None,
    ) -> tuple[int, set[str]]:
        """Delete requested records and seed existing Wallet records on initial sync."""
        deleted_id_set: set[str] = set()
        deleted_count = 0
        if purge_existing_records:
            existing_cash = self.bb_client.get_records(self.settings.bb_tr_cash_account_id)
            existing_wealth = self.bb_client.get_records(self.settings.bb_tr_wealth_account_id)
            purge_ids = [
                str(rec["id"])
                for rec in (*existing_cash, *existing_wealth)
                if rec.get("id") is not None
            ]
            if purge_ids:
                deleted_id_set.update(purge_ids)
                deleted_count = self.bb_client.delete_records(purge_ids)
        elif file_record_ids:
            deleted_id_set.update(file_record_ids)
            deleted_count = self.bb_client.delete_records(file_record_ids)

        if (
            self.db.get_metadata(BACKFILL_META_KEY) == "true"
            and self.db.count(self.BROKER_NAME) > 0
        ):
            return deleted_count, set()

        logger.info("Running initial Trade Republic existing-record seeding...")
        existing_records: list[dict[str, Any]] = self.bb_client.get_records(
            account_id=self.settings.bb_tr_cash_account_id,
            record_date_gte="2024-01-01T00:00:00Z",
        )
        seed_entries: list[SeenRecordEntry] = []
        seeded_hashes: set[str] = set()
        used_existing_ids: set[str] = set(deleted_id_set)
        for candidate in parsed_records:
            cand_dt = _parse_iso(candidate.timestamp)
            for existing in existing_records:
                ex_id = str(existing.get("id") or "")
                if ex_id and ex_id in used_existing_ids:
                    continue
                ex_amt = round(float((existing.get("amount") or {}).get("value") or 0.0), 2)
                ex_date_str = str(existing.get("recordDate") or "")
                if not ex_date_str:
                    continue
                ex_dt = _parse_iso(ex_date_str)
                if ex_amt == candidate.amount and abs(cand_dt - ex_dt) <= timedelta(days=3):
                    if ex_id:
                        used_existing_ids.add(ex_id)
                    rec_hash = compute_record_hash(
                        self.BROKER_NAME,
                        candidate.external_id,
                        candidate.timestamp,
                        candidate.amount,
                    )
                    seed_entries.append(
                        SeenRecordEntry(
                            record_hash=rec_hash,
                            broker=self.BROKER_NAME,
                            timestamp=candidate.timestamp,
                        )
                    )
                    seeded_hashes.add(rec_hash)
                    break

        if is_dry_run:
            logger.info(
                "[DRY_RUN] Would seed %d existing Trade Republic records and set %s=true in SQLite",
                len(seed_entries),
                BACKFILL_META_KEY,
            )
            return deleted_count, seeded_hashes

        if seed_entries:
            self.db.mark_seen_batch(seed_entries)
            logger.info(
                "Seeded %d existing Trade Republic records into seen_records",
                len(seed_entries),
            )

        self.db.set_metadata(BACKFILL_META_KEY, "true")
        return deleted_count, seeded_hashes

    def sync_once(
        self,
        *,
        purge_existing_records: bool = False,
        delete_file: Path | None = None,
    ) -> TRSyncSummary:
        """Run a single Trade Republic sync and portfolio balance update cycle."""
        is_dry_run = self.settings.dry_run or self.bb_client.dry_run
        file_record_ids, couch_delete_ids = load_delete_file(delete_file)
        raw_events, raw_portfolio = self.session_manager.fetch_timeline_and_portfolio()
        parsed_records = parse_tr_timeline_events(raw_events, self.settings)

        deleted_legacy, ephemeral_seeded_hashes = self._reconcile_backfill(
            parsed_records,
            is_dry_run=is_dry_run,
            purge_existing_records=purge_existing_records,
            file_record_ids=file_record_ids,
        )

        records_to_create: list[CreateRecordPayload] = []
        seen_entries: list[SeenRecordEntry] = []
        skipped_duplicates = 0

        for item in parsed_records:
            rec_hash = compute_record_hash(
                self.BROKER_NAME,
                item.external_id,
                item.timestamp,
                item.amount,
            )
            if self.db.is_seen(rec_hash) or rec_hash in ephemeral_seeded_hashes:
                skipped_duplicates += 1
                continue

            records_to_create.append(item.payload)
            seen_entries.append(
                SeenRecordEntry(
                    record_hash=rec_hash,
                    broker=self.BROKER_NAME,
                    timestamp=item.timestamp,
                )
            )

        for start in range(0, len(records_to_create), MAX_RECORDS_BATCH_SIZE):
            rec_batch = records_to_create[start : start + MAX_RECORDS_BATCH_SIZE]
            seen_batch = seen_entries[start : start + MAX_RECORDS_BATCH_SIZE]
            self.bb_client.create_records(rec_batch)
            if not is_dry_run:
                self.db.mark_seen_batch(seen_batch)

        if is_dry_run and seen_entries:
            logger.info(
                "[DRY_RUN] Skipping SQLite seen_records persistence for %d TR records",
                len(seen_entries),
            )

        patched_initial: float | None = None
        if self.couchdb_client is not None:
            couch_result = self.couchdb_client.sync_account_holdings(
                broker=self.BROKER_NAME,
                account_id=self.settings.bb_tr_wealth_account_id,
                trades=parse_tr_stock_trades(raw_events),
                live_positions=parse_tr_live_positions(raw_portfolio),
                purge_existing=purge_existing_records,
                delete_doc_ids=couch_delete_ids,
            )
            # Wallet UI subtracts BUY AssetTransaction cashAmount from REST currentBalance,
            # so REST target_balance must offset net_cash_invested_eur to keep cash = 0.00.
            patched_initial = self.bb_client.update_account_target_balance(
                account_id=self.settings.bb_tr_wealth_account_id,
                target_balance=round(couch_result.net_cash_invested_eur, 2),
            )
        else:
            portfolio_value = parse_tr_portfolio_value(raw_portfolio)
            if portfolio_value > 0:
                patched_initial = self.bb_client.update_account_target_balance(
                    account_id=self.settings.bb_tr_wealth_account_id,
                    target_balance=float(portfolio_value),
                )

        raw_cash_balance = raw_portfolio.get("cashBalance")
        if raw_cash_balance is not None and float(raw_cash_balance) >= 0.0:
            self.bb_client.update_account_target_balance(
                account_id=self.settings.bb_tr_cash_account_id,
                target_balance=round(float(raw_cash_balance), 2),
            )

        logger.info(
            "TR sync complete (dry_run=%s): created=%d, duplicates=%d, "
            "deleted_legacy=%d, patched_wealth=%s",
            is_dry_run,
            len(records_to_create),
            skipped_duplicates,
            deleted_legacy,
            patched_initial,
        )
        return TRSyncSummary(
            records_created=len(records_to_create),
            skipped_duplicates=skipped_duplicates,
            deleted_legacy_savings=deleted_legacy,
            patched_wealth_initial_balance=patched_initial,
            dry_run=is_dry_run,
        )

    def run_forever(self) -> None:
        """Continuously poll Trade Republic at configured interval."""
        run_periodic_sync(
            worker_name="Trade Republic",
            interval_seconds=self.settings.tr_poll_interval_seconds,
            sync_fn=self.sync_once,
            start_time=self.settings.sync_start_time,
            timezone_name=self.settings.sync_timezone,
        )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for tr_worker."""
    parser = argparse.ArgumentParser(description="Trade Republic to BudgetBakers sync worker")
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
        help="Delete all existing records on TR accounts before syncing",
    )
    parser.add_argument(
        "--delete-file",
        type=Path,
        default=None,
        help="Path to JSON file listing specific record_ids and couchdb_doc_ids to delete",
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
    with attach_discord_error_handler(settings, worker_name="Trade Republic"):
        try:
            db = DeduplicationStore(settings.sqlite_db_path)
            with BudgetBakersClient(
                token=settings.budgetbakers_token.get_secret_value(),
                base_url=settings.budgetbakers_base_url,
                dry_run=settings.dry_run,
                use_mock_data=settings.use_mock_data,
            ) as bb_client:
                worker = TRWorker(settings=settings, db=db, bb_client=bb_client)
                try:
                    if (
                        args.once
                        or args.dry_run_output is not None
                        or args.purge_account_records
                        or args.delete_file is not None
                    ):
                        summary = worker.sync_once(
                            purge_existing_records=args.purge_account_records,
                            delete_file=args.delete_file,
                        )
                        if args.dry_run_output is not None:
                            report: dict[str, Any] = {
                                "broker": TRWorker.BROKER_NAME,
                                "dry_run": summary.dry_run,
                                "summary": {
                                    "records_created": summary.records_created,
                                    "skipped_duplicates": summary.skipped_duplicates,
                                    "deleted_legacy_savings": summary.deleted_legacy_savings,
                                    "patched_wealth_initial_balance": (
                                        summary.patched_wealth_initial_balance
                                    ),
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
            logger.exception("Trade Republic worker failed")
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
