"""Trade Republic event listener/poller and one-time historical backfill worker."""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from config.settings import Settings, get_settings
from shared.budgetbakers import (
    MAX_RECORDS_BATCH_SIZE,
    BudgetBakersClient,
    CreateRecordPayload,
)
from shared.database import DeduplicationStore, SeenRecordEntry, compute_record_hash

from tr_worker.event_parser import (
    ParsedTRRecord,
    TRRecordKind,
    parse_tr_portfolio_value,
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

    def close(self) -> None:
        """Close owned Discord 2FA HTTP client resources."""
        if self._owns_session_manager:
            self.session_manager.discord_client.close()

    def _reconcile_backfill(
        self,
        parsed_records: list[ParsedTRRecord],
    ) -> int:
        """Delete manual -50 EUR savings plans once and seed existing manual interest."""
        if self.db.get_metadata(BACKFILL_META_KEY) == "true":
            return 0

        logger.info("Running one-time Trade Republic historical backfill reconciliation...")
        # 1. Find and delete manual -50 EUR "Savings plan" placeholder records on TR cash account
        existing_inv = self.bb_client.get_records(
            account_id=self.settings.bb_tr_cash_account_id,
            category_id=self.settings.bb_category_investments,
            record_date_gte="2024-01-01T00:00:00Z",
        )
        legacy_ids: list[str] = []
        for rec in existing_inv:
            rec_id = str(rec.get("id") or "")
            note = str(rec.get("note") or "")
            amt_val = float((rec.get("amount") or {}).get("value") or 0.0)
            if rec_id and amt_val == -50.0 and "sh " not in note:
                legacy_ids.append(rec_id)

        deleted_count = 0
        if legacy_ids:
            deleted_count = self.bb_client.delete_records(legacy_ids)
            logger.info("Deleted %d legacy manual Savings plan placeholder records", deleted_count)

        # 2. Match existing manual Interest records on TR cash account (±3 days + same amount)
        existing_interest: list[dict[str, Any]] = self.bb_client.get_records(
            account_id=self.settings.bb_tr_cash_account_id,
            category_id=self.settings.bb_category_interest_dividends,
            record_date_gte="2024-01-01T00:00:00Z",
        )
        seed_entries: list[SeenRecordEntry] = []
        for candidate in parsed_records:
            if candidate.kind != TRRecordKind.INTEREST:
                continue
            cand_dt = _parse_iso(candidate.timestamp)
            for existing in existing_interest:
                ex_amt = round(float((existing.get("amount") or {}).get("value") or 0.0), 2)
                ex_date_str = str(existing.get("recordDate") or "")
                if not ex_date_str:
                    continue
                ex_dt = _parse_iso(ex_date_str)
                if ex_amt == candidate.amount and abs(cand_dt - ex_dt) <= timedelta(days=3):
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
                    break

        if seed_entries:
            self.db.mark_seen_batch(seed_entries)
            logger.info(
                "Seeded %d existing manual Trade Republic interest records into seen_records",
                len(seed_entries),
            )

        self.db.set_metadata(BACKFILL_META_KEY, "true")
        return deleted_count

    def sync_once(self) -> TRSyncSummary:
        """Run a single Trade Republic sync and portfolio balance update cycle."""
        raw_events, raw_portfolio = self.session_manager.fetch_timeline_and_portfolio()
        parsed_records = parse_tr_timeline_events(raw_events, self.settings)

        deleted_legacy = self._reconcile_backfill(parsed_records)

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
            if self.db.is_seen(rec_hash):
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
            self.db.mark_seen_batch(seen_batch)

        portfolio_value = parse_tr_portfolio_value(raw_portfolio)
        patched_initial: float | None = None
        if portfolio_value > 0:
            patched_initial = self.bb_client.update_account_target_balance(
                account_id=self.settings.bb_tr_wealth_account_id,
                target_balance=float(portfolio_value),
            )

        logger.info(
            "TR sync complete: created=%d, duplicates=%d, deleted_legacy=%d, patched_wealth=%s",
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
        )

    def run_forever(self) -> None:
        """Continuously poll Trade Republic at configured interval."""
        logger.info(
            "Starting Trade Republic worker loop (interval=%ds)",
            self.settings.tr_poll_interval_seconds,
        )
        while True:
            try:
                self.sync_once()
            except Exception:
                logger.exception("Error during Trade Republic sync iteration")
            time.sleep(self.settings.tr_poll_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for tr_worker."""
    parser = argparse.ArgumentParser(description="Trade Republic to BudgetBakers sync worker")
    parser.add_argument("--once", action="store_true", help="Run a single sync cycle and exit")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    db = DeduplicationStore(settings.sqlite_db_path)
    with BudgetBakersClient(
        token=settings.budgetbakers_token.get_secret_value(),
        base_url=settings.budgetbakers_base_url,
        dry_run=settings.dry_run,
    ) as bb_client:
        worker = TRWorker(settings=settings, db=db, bb_client=bb_client)
        try:
            if args.once:
                worker.sync_once()
            else:
                worker.run_forever()
        finally:
            worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
