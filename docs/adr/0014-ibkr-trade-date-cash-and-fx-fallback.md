# ADR-0014: IBKR Trade-Date `endingCash` Reconciliation and Foreign Trade FX Fallback

- **Status**: Accepted
- **Date**: 2026-11-21
- **Deciders**: User, Antigravity Agent
- **Related**: [ADR-0006](./0006-ibkr-multi-currency-fx-filtering-and-purge.md), [ADR-0007](./0007-couchdb-stock-holdings-and-paired-transfers.md)

## Context

1. **Unsettled Trade Double-Counting (`endingSettledCash` vs. `endingCash`)**:
   - In IBKR Activity Flex Query XML, `<CashReportCurrency currency="BASE_SUMMARY">` provides both `endingCash` (trade-date cash, reduced immediately when an order executes) and `endingSettledCash` (settled cash, which excludes unsettled trades whose `settleDateTarget` is after `toDate`).
   - Meanwhile, `<Trades>` and `<OpenPositions>` in the same statement reflect executions immediately on trade date (T+0). Consequently, `CouchDBStockSyncClient.sync_account_holdings()` creates `-AssetTransaction` documents and includes unsettled purchases in `net_cash_invested_eur` right away, while BudgetBakers Wallet adds the position's market value to Portfolio Value.
   - Previously, `parse_flex_xml()` prioritized `endingSettledCash` over `endingCash` (and used Python `or`, which would also fall back if `endingCash` equaled `0`). During a T+1/T+2 settlement window (e.g., a newly executed ETF buy), `IBKRWorker.sync_once()` added `net_cash_invested_eur` onto `endingSettledCash`, double-counting the unsettled purchase across both **Available Cash** and **Portfolio Value** in BudgetBakers Wallet.
2. **Missing `fxRateToBase` on Foreign-Currency `<Trade>` Rows**:
   - IBKR Flex `<Trade>` elements for non-EUR securities (e.g., `USD` stocks) may omit `fxRateToBase`, even when the same statement includes `assetCategory="CASH"` FX conversions (`EUR.USD`) on the execution date and `OpenPositions.fxRateToBase`.
   - Previously, `parse_flex_xml()` defaulted missing `Trade.fxRateToBase` to `1` instead of converting to `EUR`, storing foreign-currency `netCash` 1:1 as EUR `cashAmount` in CouchDB.

## Decision

1. **Prefer Trade-Date `endingCash` (`ibkr_worker/flex_client.py`)**:
   - In `parse_flex_xml()`, read `endingCash` first using explicit `is not None` checking (preserving `0.00` when fully invested), and fall back to `endingSettledCash` only when `endingCash` is absent.
2. **Resolve Missing `Trade.fxRateToBase` via Shared FX Helper (`ibkr_worker/flex_client.py`)**:
   - Parse `OpenPositions` and index `assetCategory="CASH"` (`EUR.<CCY>`) FX conversions by `(currency, date)` (keeping the largest conversion by `abs(quantity)` on each date) and by currency in `stmt_fx_rates` before parsing security `Trades` and `CashTransactions`.
   - Share `_resolve_fx_rate_to_base()` across both `Trades` and `CashTransactions`: use explicit positive `fxRateToBase` when present, otherwise same-date `CASH` FX conversion rate, otherwise `stmt_fx_rates[currency]` (from `OpenPositions` or statement `CASH` trades), defaulting to `1`.

## Consequences

- **Positive**: BudgetBakers Wallet's **Available Cash** and total account balance remain exact immediately after executing IBKR trades during T+1/T+2 settlement windows, and non-EUR IBKR trades lacking `fxRateToBase` record accurate EUR `cashAmount` values in CouchDB (updating existing `finance-sync-tx` documents in-place on the next sync).
- **Negative / Trade-offs**: While a trade is unsettled, BudgetBakers Wallet displays IBKR's trade-date buying-power cash (`endingCash`) rather than withdrawable settled cash (`endingSettledCash`), matching IBKR's live portfolio cash view.
