# ADR 0001: SQLite WAL Mode and SHA-256 Deduplication

## Status
Accepted

## Context
Both `ibkr_worker` and `tr_worker` run as separate containers mounting the same `/app/data` volume and need a lightweight, zero-dependency persistence layer to guarantee at-most-once delivery of financial records to the BudgetBakers Wallet REST API (which does not enforce client-side idempotency keys on `POST /v1/api/records`).

## Decision
1. Use a shared SQLite database (`finance_sync.db`) with `PRAGMA journal_mode=WAL;` and `PRAGMA busy_timeout=5000;` enabled on every connection.
2. Store seen transactions in `seen_records (hash TEXT PRIMARY KEY, broker TEXT, timestamp DATETIME)`.
3. Compute `hash` deterministically as:
   `sha256(broker + external_id + timestamp + f"{float(amount):.2f}")`.
4. Store one-time migration flags (such as Trade Republic initial backfill completion) in a `sync_metadata (key TEXT PRIMARY KEY, value TEXT)` table.

## Consequences
- Zero external database containers required in Portainer.
- Concurrent reads/writes between `ibkr_worker` and `tr_worker` succeed without table-lock errors.
- Repeated polling of overlapping IBKR Flex statements or Trade Republic timelines is strictly idempotent.
