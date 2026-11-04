# ADR 0005: Read-Only Dry-Run Mode and Preview Reporting

## Status
Accepted

## Context
Users running `finance-sync` locally or in production need a safe way to preview what records will be created, which legacy `-50 EUR` savings plan placeholders will be deleted, which manual interest entries will be matched during initial backfill, and what `initialBalance` adjustments will be computed—without mutating either the live BudgetBakers Wallet REST API or the local SQLite deduplication database (`seen_records` and `sync_metadata`).

Previously, setting `DRY_RUN=true` intercepted `POST`, `DELETE`, and `PATCH` requests in `BudgetBakersClient`, but:
1. `IBKRWorker` and `TRWorker` still executed `db.mark_seen_batch(...)` and `db.set_metadata("tr_backfill_reconciled_v1", "true")`, causing a dry run to permanently mark unseen transactions as synced in SQLite.
2. `BudgetBakersClient.get_account()` and `get_records()` unconditionally returned synthetic empty stubs when `dry_run=True`, preventing accurate previews of `initialBalance` adjustments and historical backfill matching against a live Wallet account.
3. There were no `--dry-run` or `--dry-run-output` CLI flags for ad-hoc inspection and JSON report export.

## Decision
1. **Zero SQLite Mutations in Dry-Run Mode**:
   - When `dry_run=True` (via `DRY_RUN=true`, `--dry-run`, or `--dry-run-output`), `IBKRWorker` and `TRWorker` read from `DeduplicationStore` (`is_seen`, `get_metadata`) to accurately identify unseen transactions, but skip all writes (`mark_seen_batch`, `set_metadata`).
   - During `TRWorker._reconcile_backfill`, matched manual interest record hashes are returned in an ephemeral in-memory set so the dry-run pass treats them as reconciled duplicates without persisting them to `seen_records`.
2. **Live Read-Only BudgetBakers `GET` Queries with Mock-Data Fallback**:
   - `BudgetBakersClient.get_account()` and `get_records()` perform live read-only `GET /v1/api/accounts` and `GET /v1/api/records` calls even when `dry_run=True` so `initialBalance` calculations and backfill matching reflect real account state.
   - When `use_mock_data=True` (or the token is the default `"mock-budgetbakers-token"`) and no HTTP mock/server is reachable, `get_account()` and `get_records()` gracefully fall back to synthetic stub responses so offline mock dry runs (`USE_MOCK_DATA=true DRY_RUN=true`) work out-of-the-box without network access.
   - All mutating endpoints (`POST /v1/api/records`, `DELETE /v1/api/records`, `PATCH /v1/api/accounts`) remain strictly blocked in dry-run mode and record their payloads in `dry_run_created`, `dry_run_deleted_ids`, and `dry_run_patched_accounts`.
3. **CLI Flags & Structured JSON Report (`--dry-run` & `--dry-run-output`)**:
   - Both `ibkr_worker.worker` and `tr_worker.worker` accept `--dry-run` and `--dry-run-output <path.json>`.
   - Passing `--dry-run-output <path.json>` implies `--dry-run` and `--once`, exporting a deterministic JSON preview report containing summary metrics and the exact payloads that would be created, deleted, or patched.

## Consequences
- Running a dry run against either mock fixtures or a live BudgetBakers account is 100% side-effect-free across both remote APIs and local SQLite state.
- Subsequent live runs (`DRY_RUN=false`) still process all unseen records normally.
- Operators can inspect human-readable `[DRY_RUN]` logs or machine-readable JSON reports before enabling live sync.
