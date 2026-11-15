# ADR 0006: IBKR Multi-Currency Valuation, FX Conversion Trade Filtering, and Account Record Purge

## Status
Accepted

## Context
When parsing real Interactive Brokers Activity Flex Query XML statements for an account with a `EUR` base currency holding both `EUR` and `USD` securities:
1. `OpenPositions` reports each security's `positionValue` (`quantity * markPrice`) in the security's native trading currency (e.g., `USD`), along with `fxRateToBase` (`1` for `EUR`, `<1` for `USD` -> `EUR`). Summing raw `positionValue` without applying `fxRateToBase` misstates the `EUR` portfolio market value used to patch BudgetBakers Wallet's `initialBalance`.
2. In `<Trades>`, IBKR logs automatic currency exchanges (`EUR.USD`) under `assetCategory="CASH"` with `netCash="0"` and non-zero `proceeds` whenever a foreign-currency security is bought or a foreign-currency dividend is swept to base currency. Treating `assetCategory="CASH"` rows as security trades creates synthetic `BUY`/`SELL EUR.USD` records in BudgetBakers Wallet that distort income/expense totals.
3. Users migrating an existing BudgetBakers Wallet investment account with inaccurate manual history need a one-time mechanism to purge existing records on `BB_IBKR_ACCOUNT_ID` before importing the authoritative IBKR Flex history.

## Decision
1. **Base-Currency Conversion for `OpenPositions`**:
   - In `ibkr_worker/flex_client.py` (`parse_flex_xml`), use `positionValueInBase` when present and non-zero; otherwise multiply `positionValue` (or `position * markPrice`) by `fxRateToBase` (defaulting to `1` when omitted) and quantize both `open_positions_value` and `ending_cash` to `0.01` (`ROUND_HALF_UP`).
2. **Exclude `assetCategory="CASH"` and Zero-Rounded Trades**:
   - Skip `<Trade>` elements where `assetCategory == "CASH"` or where `net_cash` rounds to `0.00` (`ROUND_HALF_UP`).
3. **One-Time Account Record Purge (`--purge-account-records`)**:
   - Add `--purge-account-records` (`purge_existing_records=True`) to `ibkr_worker`, which queries `GET /v1/api/records?accountId={BB_IBKR_ACCOUNT_ID}` and deletes existing record IDs via `DELETE /v1/api/records` in chunks of `<=10` (or records them in `would_delete_record_ids` without mutating Wallet when `--dry-run` is active).

## Consequences
- Multi-currency IBKR portfolios (`EUR` + `USD`, etc.) produce an exact 2-decimal `EUR` `total_portfolio_value` when patching BudgetBakers `initialBalance`.
- Internal `EUR.USD` FX conversion rows in `<Trades>` are ignored while all real stock/ETF executions (`assetCategory="STK"`, etc.) are synced.
- Operators can safely preview and execute a clean account reset with `--dry-run --purge-account-records` before live deployment.
