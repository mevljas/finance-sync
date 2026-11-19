# Product Requirements Document (PRD): `finance-sync`

## 1. Problem Statement
Managing personal finances across multiple European brokers and fintech platforms—specifically **Interactive Brokers (IBKR)** and **Trade Republic (TR)**—creates manual bookkeeping overhead in **BudgetBakers Wallet** because neither IBKR nor Trade Republic's investment/card timeline is natively supported by BudgetBakers bank-sync with rich investment metadata (ISIN, executed shares, execution price, and live portfolio mark-to-market valuation).

## 2. Goals & Objectives
1. **IBKR Ingestion**: Fetch and parse Activity Flex Query XML statements (`Trades`, `CashTransactions`, `OpenPositions`, `CashReport`) via IBKR Flex Web Service two-step polling (`SendRequest` -> wait 10s -> `GetStatement`) using `ibflex2`.
2. **Trade Republic Ingestion**: Fetch and parse timeline events (card debits, monthly interest, 1% Saveback bonus income, savings plans, stock buys, dividends, SEPA cash transfers) and live portfolio valuation via `pytr`, with an interactive, ready-first, zero-timeout **Discord Gateway Bot** fallback for 2FA session renewal.
3. **Deduplication & Reconciliation**: Deduplicate all ingested transactions in a shared local SQLite WAL database (`seen_records`) using `sha256(broker + external_id + timestamp + normalized_amount)` and reconcile against BudgetBakers Wallet REST API (`POST /v1/api/records` in batches of `<=20`, `PATCH /v1/api/accounts` for live portfolio valuation).
4. **Homelab Containerization**: Package into an ultra-minimal Alpine Linux Docker image (`python:3.12-alpine` + `uv`) deployed via a Portainer-compatible `docker-compose.yml` stack.

## 3. Account & Category Mapping Behavior

### 3.1 Interactive Brokers (`BB_IBKR_ACCOUNT_ID`)
- **Stock/ETF Buys & Sells (`Trades`)**: Logged as `expense` (buy) or `income` (sell) records in category `Fin. investments` (`5c5c232a-005a-8000-8000-000000000000`). The `note` field embeds executed shares, ISIN, symbol, and price:
  `BUY 5.0000 sh XF0000000001 (FAKE) @ 100.00 EUR`
- **Dividends & Broker Interest (`CashTransactions`)**: Logged in category `Interest, dividends` (`5c5c2711-0064-8000-8000-000000000000`).
- **Cash Deposits/Withdrawals**: Skipped on IBKR because Revolut bank-sync in BudgetBakers already captures and pairs SEPA transfers to IBKR.
- **Portfolio Valuation (`OpenPositions` + `CashReport`)**: Patched onto `BB_IBKR_ACCOUNT_ID` via `PATCH /v1/api/accounts` by adjusting `initialBalance` so `currentBalance` equals total live portfolio value.

### 3.2 Trade Republic (`BB_TR_CASH_ACCOUNT_ID` & `BB_TR_WEALTH_ACCOUNT_ID`)
- **Card Payments**: Logged as `expense` records on `BB_TR_CASH_ACCOUNT_ID` for events after `TR_CARD_CUTOFF_DATE`, categorized automatically using merchant keyword + MCC rules with fallback to `Unknown expense` (`5c5c32c9-0082-8000-8000-000000000000`).
- **Monthly Interest**: Logged as `income` (`Interest, dividends`) on `BB_TR_CASH_ACCOUNT_ID`. During initial backfill, existing records in Wallet are matched by `(amount, date ±3 days)` so they are seeded into `seen_records` and not duplicated.
- **SEPA Cash Deposits/Withdrawals**: Logged on `BB_TR_CASH_ACCOUNT_ID` under category `Transfer` (`5c5c4e21-00c8-8000-8000-000000000000`).
- **1% Saveback**: Synced as a native `-AssetTransaction` stock purchase in CouchDB on `BB_TR_WEALTH_ACCOUNT_ID` when CouchDB is configured (or as a REST `Interest, dividends` record otherwise).
- **Savings Plans & Stock Buys**:
  - Supports deleting all existing records on the account (`--purge-account-records`) or deleting specific record/CouchDB IDs from a local gitignored JSON file (`--delete-file <path.json>`).
  - Creates a paired `Transfer` (`transfer.pairingMode = "new"`) from `BB_TR_CASH_ACCOUNT_ID` to `BB_TR_WEALTH_ACCOUNT_ID` and syncs the stock purchase as an `-AssetTransaction` in CouchDB when CouchDB is configured (or logs REST `Fin. investments` records otherwise).
- **Portfolio Valuation**: Reconciles `BB_TR_WEALTH_ACCOUNT_ID` uninvested cash balance to `0.00 EUR` when CouchDB tracks stock holdings (or patches total live portfolio market value onto `BB_TR_WEALTH_ACCOUNT_ID` via `PATCH /v1/api/accounts` in REST-only mode).

## 4. Non-Functional Requirements
- **Zero Secrets**: No credentials or personal UUIDs in git history.
- **Mock Mode**: Full support for `USE_MOCK_DATA=true` and `DRY_RUN=true` for offline testing and CI verification.
- **Polling Intervals**: IBKR every 6 hours (`21600s`), Trade Republic every 15 minutes (`900s`).
