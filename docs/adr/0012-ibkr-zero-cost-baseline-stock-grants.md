# ADR-0012: Zero-Cost IBKR Baseline Positions for Free Onboarding Stock Grants

- **Status**: Accepted
- **Date**: 2026-11-21
- **Deciders**: User, Antigravity Agent
- **Related**: [ADR-0007](./0007-couchdb-stock-holdings-and-paired-transfers.md), [ADR-0008](./0008-ibkr-historical-xml-import.md)

## Context

Under [ADR-0007](./0007-couchdb-stock-holdings-and-paired-transfers.md), `CouchDBStockSyncClient.sync_account_holdings()` creates a synthetic `finance-sync-baseline` `-AssetTransaction` document dated `2025-01-01T00:00:00.000Z` whenever a broker's live `OpenPositions` quantity exceeds the sum of synced `finance-sync-tx` trades. Previously, every `finance-sync-baseline` document valued those shares at the position's current mark price (`price = max(1, round(unit_price * 100))`, `cashAmount = max(1, round(baseline_shares * unit_price_eur * 100))`).

On Interactive Brokers (`ibkr_worker`), some unmatched `OpenPositions` shares are paid pre-window holdings or broker-transferred ETFs (such as `VWCE` transferred from Trading 212) that *must* retain a paid mark-price baseline, whereas `IBKR` shares awarded via Interactive Brokers' onboarding/referral bonus have no paid `<Trade>` in Flex XML and were falsely shown as a cash purchase (`Available cash -€437.93 -> Interactive Brokers Group Inc.`).

## Decision

1. **Targeted Zero-Cost Baseline Allowlist (`IBKR_ZERO_COST_BASELINE_ASSETS`)**:
   - `config/settings.py` defines `IBKR_ZERO_COST_BASELINE_ASSETS` (comma-separated symbols or ISINs, defaulting to `"IBKR"`), exposed in `docker-compose.yml` and `.env.example`, and parsed via `Settings.zero_cost_baseline_asset_set`.
2. **Selective Zero-Cost Matching (`shared/couchdb.py` + `ibkr_worker/worker.py`)**:
   - `CouchDBStockSyncClient.sync_account_holdings()` accepts `zero_cost_baseline_assets: Collection[str] = ()` (and `zero_cost_baseline: bool = False`).
   - Only `finance-sync-baseline` positions whose `symbol` or `isin` matches `zero_cost_baseline_assets` (case-insensitively) are written with `price = 0` and `cashAmount = 0`. All other baseline positions (such as transferred-in `VWCE`) retain their paid mark-price baseline.
   - `IBKRWorker.sync_once()` passes `zero_cost_baseline_assets=self.settings.zero_cost_baseline_asset_set`, while `TRWorker.sync_once()` keeps the default empty allowlist.
3. **Automatic In-Place Migration & Balance Reconciliation**:
   - Because `sync_account_holdings()` compares `"cashAmount"` and `"price"` against existing CouchDB documents, an existing paid `finance-sync-baseline` document for a listed asset (e.g., `IBKR`) is updated in-place (`_rev` preserved) on the next sync without requiring `--purge-account-records`.
   - Removing `cashAmount` from the gifted baseline reduces `net_cash_invested_eur`, and `PATCH /v1/api/accounts` adjusts `initialBalance` accordingly so `Available Cash`, `Portfolio Balance`, and `Account Balance` in Wallet remain exact.
   - If the user later sells part of a zero-cost baseline position, `baseline_shares = pos.quantity - synced_shares` stays equal to the full gifted grant at `€0.00` cost while the `SELL` transaction records actual sale proceeds.

## Consequences

- **Positive**: Free `IBKR` onboarding/referral stock grants display `€0.00` cash spent and `$0.00` cost per share while transferred-in paid holdings (e.g., `VWCE`) keep their mark-price baseline untouched, and additional free symbols/ISINs can be added via `IBKR_ZERO_COST_BASELINE_ASSETS`.
- **Negative / Trade-offs**: Assumes any unmatched `finance-sync-baseline` position matching `IBKR_ZERO_COST_BASELINE_ASSETS` is a free grant (paid shares of the same ticker acquired via `<Trades>` are synced separately as `finance-sync-tx` documents). Also assumes BudgetBakers Wallet handles `0`-cent `price`/`cashAmount` cleanly (falling back to `1` cent is a one-line change if needed).
