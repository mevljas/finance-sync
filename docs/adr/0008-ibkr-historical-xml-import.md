# ADR-0008: Historical IBKR Activity Flex XML Import via `--xml-file`

- **Status**: Accepted
- **Date**: 2026-11-16
- **Deciders**: User, Antigravity Agent

## Context

Interactive Brokers Activity Flex Queries via the automated Flex Web Service API support preset periods up to `Last 365 Calendar Days`, whereas the IBKR Client Portal UI allows running one-off custom date ranges for earlier years (e.g., `2025.xml`, `2026.xml`) and downloading them as `.xml` files. Once historical years are imported, the scheduled Flex Web Service query only needs to fetch recent data (e.g., `Last 30 Calendar Days`).

## Decision

1. **CLI Flag `--xml-file <file_or_dir> [...]` (`ibkr_worker/worker.py`)**:
   - `ibkr_worker` accepts one or more file paths or directory paths via `--xml-file`.
   - When a directory path is passed, `_resolve_xml_files()` expands all `*.xml` files inside the directory in sorted order.
2. **Chronological Statement Merging (`merge_statement_data` in `ibkr_worker/flex_client.py`)**:
   - Each XML file is parsed into `IBKRStatementData` including its `<FlexStatement toDate="...">` attribute.
   - `merge_statement_data()` orders statements chronologically by `to_date`, merges and deduplicates `Trades` (by `trade_id`) and `CashTransactions` (by `transaction_id`), and takes `OpenPositions`, `open_positions_value`, and `ending_cash` from the statement with the latest `to_date`.
3. **Default Live Flex Polling**:
   - When `--xml-file` is omitted, `ibkr_worker` polls the live IBKR Flex Web Service API (configured for `Last 30 Calendar Days`). Because `CouchDBStockSyncClient` preserves all previously synced `reservedSource: "finance-sync-tx"` trade documents in CouchDB and `DeduplicationStore` persists all `seen_records` in SQLite, subsequent 30-day API runs seamlessly append new trades without losing historical data.

## Consequences

- **Positive**: Multi-year IBKR history (`2025.xml`, `2026.xml`, etc.) can be imported in a single command (`--xml-file /app/input/2025.xml /app/input/2026.xml` or `--xml-file /app/input`), while keeping routine scheduled Flex polling fast and lightweight (`Last 30 Calendar Days`).
- **Negative / Trade-offs**: None; fully backward-compatible when `--xml-file` is omitted.
