# ADR-0007: Native Stock Holdings Sync via CouchDB and Paired Transfers for Trade Republic Savings Plans

- **Status**: Accepted
- **Date**: 2026-11-16
- **Deciders**: User, Antigravity Agent

## Context

BudgetBakers Wallet provides a built-in **Investments / Stocks** price tracker that automatically values held shares using daily market price history (`investments.getStockPriceHistory`). However, BudgetBakers' public REST API (`https://rest.budgetbakers.com/wallet/v1/api/*`) only exposes cash `Record` (`-Record_<uuid>`) and `Account` (`-Account_<uuid>`) endpoints — it does not expose endpoints for stock holdings.

Inspection of the BudgetBakers web application's replication layer revealed that native stock holdings are stored in the user's CouchDB instance (`https://couch-prod-eu-2.budgetbakers.com/bb-<owner_id>`) using two document types:
1. **`-StocksFundsAsset_<uuid>`** (`reservedModelType: "StocksFundsAsset"`): Defines a security's ticker symbol, exchange (`exchangeCode`, e.g., `XBER`, `XETR`, `XNGS`), and quote currency (`currencyCode`, e.g., `EUR`, `USD`).
2. **`-AssetTransaction_<uuid>`** (`reservedModelType: "AssetTransaction"`): Logs an individual stock purchase (`transactionType: 0`) or sale (`transactionType: 1`) on an investment account (`accountId: "-Account_<uuid>"`), with fractional `quantity`, `price` (in integer cents of `priceCurrencyCode`), and `cashAmount` (in integer EUR cents).

Additionally, for Trade Republic Savings Plans, the outgoing cash deduction on `BB_TR_CASH_ACCOUNT_ID` is created as a **paired `Transfer`** to `BB_TR_WEALTH_ACCOUNT_ID`, while the stock purchase itself is tracked as an `-AssetTransaction` in CouchDB so Wallet tracks live stock prices automatically without double-counting cash expenses.

## Decision

1. **`CouchDBStockSyncClient` (`shared/couchdb.py`)**:
   - When `BB_COUCHDB_USER` and `BB_COUCHDB_PASSWORD` are configured (`settings.has_couchdb == True`), both `ibkr_worker` and `tr_worker` synchronize stock purchases and live positions into CouchDB via `_all_docs` and `_bulk_docs`.
   - Each synced broker trade (`IBKR` stock buy/sell, `Trade Republic` Savings Plan execution, and `Trade Republic` `1% Saveback` bonus share purchase) is written as a deterministic `-AssetTransaction_<uuid5>` document (`reservedSource: "finance-sync-tx"`).
   - For any asset where the broker's live open position (`quantity`) exceeds the sum of synced transaction shares (e.g., shares purchased before the historical query window), a single deterministic baseline document (`reservedSource: "finance-sync-baseline"`, dated `2025-01-01T00:00:00.000Z`) is upserted with `baseline_shares = live_broker_shares - sum(synced_tx_shares)` so the total shares in BudgetBakers Wallet match the broker's live position to the exact fractional share.
   - Deleting existing `-AssetTransaction` documents or REST `Record`s is controlled generically via `--purge-account-records` (delete all on account) or `--delete-file <path.json>` (delete specific IDs from a local gitignored file).

2. **Paired Transfers for Trade Republic Savings Plans**:
   - When `settings.has_couchdb == True`, `parse_tr_timeline_events()` emits each Trade Republic Savings Plan on `BB_TR_CASH_ACCOUNT_ID` with `transfer: {"pairingMode": "new", "accountId": "<BB_TR_WEALTH_ACCOUNT_ID>"}` and omits `categoryId` (as required by BudgetBakers' REST schema when `transfer` is set).
   - BudgetBakers automatically creates the paired incoming transfer on `BB_TR_WEALTH_ACCOUNT_ID`.
   - `1% Saveback` is paid directly in fractional shares by Trade Republic and does not deduct from `BB_TR_CASH_ACCOUNT_ID`, so it is written exclusively as a CouchDB `-AssetTransaction` on `BB_TR_WEALTH_ACCOUNT_ID`.

3. **Uninvested Cash Balance Reconciliation**:
   - Because CouchDB `-AssetTransaction` documents automatically track the market value of the stock holdings in Wallet, the REST account cash balance (`PATCH /v1/api/accounts`) reconciles only the **uninvested cash balance** when `settings.has_couchdb == True`:
     - `BB_IBKR_ACCOUNT_ID`: Reconciled to `statement.ending_cash` (settled uninvested cash in EUR).
     - `BB_TR_WEALTH_ACCOUNT_ID`: Reconciled to `0.00 EUR` (offsetting the incoming transfer mirrors that were immediately invested into stocks).
   - When `settings.has_couchdb == False`, the legacy REST-only behavior is preserved for backward compatibility.

## Consequences

- **Positive**: BudgetBakers Wallet's native Investments / Stocks tracker displays exact share counts and tracks live market prices automatically, while Savings Plan funding is cleanly linked as a paired Transfer and the repository remains 100% generic for any user.
- **Negative / Trade-offs**: Requires the user's CouchDB basic-auth credentials (`BB_COUCHDB_USER` and `BB_COUCHDB_PASSWORD`, stored as `SecretStr` in `.env`) alongside the REST API token.
