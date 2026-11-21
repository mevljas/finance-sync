# ADR-0013: Zero-Cost CouchDB Asset Transactions for Trade Republic 1% Saveback

- **Status**: Accepted
- **Date**: 2026-11-21
- **Deciders**: User, Antigravity Agent
- **Related**: [ADR-0007](./0007-couchdb-stock-holdings-and-paired-transfers.md), [ADR-0012](./0012-ibkr-zero-cost-baseline-stock-grants.md)

## Context

Under [ADR-0007](./0007-couchdb-stock-holdings-and-paired-transfers.md), when CouchDB stock holdings sync is enabled (`settings.has_couchdb == True`), Trade Republic `1% Saveback` bonus share purchases are omitted from REST records and written exclusively as CouchDB `finance-sync-tx` `-AssetTransaction` documents on `BB_TR_WEALTH_ACCOUNT_ID`.

Previously, `parse_tr_stock_trades()` populated Saveback `StockTradeInput` with `price` equal to the execution price and `cash_amount_eur` equal to the Saveback reward (up to `€15.00`), and `CouchDBStockSyncClient.sync_account_holdings()` clamped `price` and `cashAmount` to `max(1, ...)`. Even though `TRWorker` offset `net_cash_invested_eur` via `initialBalance`, BudgetBakers Wallet displayed each Saveback transaction in the stock holding modal as a cash expenditure (`Available cash -€15.00 -> <Stock>`) rather than free shares (`€0.00` cost).

## Decision

1. **Zero-Cent Support on `StockTradeInput` (`shared/couchdb.py`)**:
   - Relax the cent floor on `trades` in `CouchDBStockSyncClient.sync_account_holdings()` from `max(1, ...)` to `max(0, ...)` so free acquisitions can persist `price: 0` and `cashAmount: 0`.
2. **Zero-Cost Saveback Emissions (`tr_worker/event_parser.py`)**:
   - In `parse_tr_stock_trades()`, emit `price=Decimal("0")` and `cash_amount_eur=Decimal("0")` whenever `is_saveback` is `True`, while keeping execution `price` and `abs(raw_amount)` for paid Savings Plans and Buy orders.
   - Continue omitting REST cash records for Saveback when `settings.has_couchdb == True` (no extra `+€15.00` REST income record); preserve the legacy REST-only `Interest, dividends` record fallback when `settings.has_couchdb == False`.
3. **Automatic In-Place Migration**:
   - Because `TRWorker` fetches the full Trade Republic timeline and generates deterministic `<event_id>:saveback` CouchDB document IDs, existing paid Saveback `-AssetTransaction` documents are automatically updated in-place (`_rev` preserved) to `cashAmount: 0, price: 0` on the next sync, and `BB_TR_WEALTH_ACCOUNT_ID`'s `initialBalance` is adjusted so uninvested cash stays `0.00 EUR`.

## Consequences

- **Positive**: Trade Republic `1% Saveback` bonus shares render identically to free IBKR onboarding stock grants (`€0.00` cash spent, `€0.00` cost per share), and historical Saveback records migrate automatically without `--purge-account-records`.
- **Negative / Trade-offs**: Saveback reward amounts are not separately tallied as cash income in Wallet's income category charts when CouchDB sync is active (they appear purely as zero-cost share appreciation in Portfolio).
