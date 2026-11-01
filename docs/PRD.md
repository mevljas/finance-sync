# Product Requirements Document (PRD): `finance-sync`

## 1. Problem Statement
Managing personal finances across multiple European brokers and fintech platforms—specifically **Interactive Brokers (IBKR)** and **Trade Republic (TR)**—creates manual bookkeeping overhead in **BudgetBakers Wallet** because neither IBKR nor Trade Republic's investment/card timeline is natively supported by BudgetBakers bank-sync with rich investment metadata (ISIN, executed shares, execution price, and live portfolio mark-to-market valuation).

## 2. Goals & Objectives
1. **IBKR Ingestion**: Fetch and parse Activity Flex Query XML statements (`Trades`, `CashTransactions`, `OpenPositions`, `CashReport`) via IBKR Flex Web Service two-step polling (`SendRequest` -> wait 10s -> `GetStatement`) using `ibflex2`.
2. **Trade Republic Ingestion**: Fetch and parse timeline events (card debits, monthly interest, 1% Saveback bonus income, savings plans, stock buys, dividends, SEPA cash transfers) and live portfolio valuation via `pytr`, with an interactive **Discord Bot** fallback for 2FA session renewal.
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
- **Card Payments**: Logged as `expense` records on `BB_TR_CASH_ACCOUNT_ID` for events after `TR_CARD_CUTOFF_DATE` (`2026-11-01T09:00:00Z`), categorized automatically using merchant keyword + MCC rules with fallback to `Unknown expense` (`5c5c32c9-0082-8000-8000-000000000000`).
- **Monthly Interest**: Logged as `income` (`Interest, dividends`) on `BB_TR_CASH_ACCOUNT_ID`. During initial backfill, existing manual interest entries in Wallet are matched by `(amount, date ±3 days)` so they are not duplicated while missing historical months are created.
- **SEPA Cash Deposits/Withdrawals**: Logged on `BB_TR_CASH_ACCOUNT_ID` under category `Transfer` (`5c5c4e21-00c8-8000-8000-000000000000`).
- **1% Saveback**: Logged as bonus `income` (`Interest, dividends`) on `BB_TR_WEALTH_ACCOUNT_ID` with executed shares, ISIN, and price in `note`.
- **Savings Plans & Stock Buys**:
  - During initial backfill, deletes existing placeholder `-50 EUR` `"Savings plan"` entries on `BB_TR_CASH_ACCOUNT_ID` (via `DELETE /v1/api/records`) once.
  - Deducts the cash amount as an `expense` (`Fin. investments`) on `BB_TR_CASH_ACCOUNT_ID` AND logs the stock buy record (`Fin. investments`, with executed shares, ISIN, and price in `note`) on `BB_TR_WEALTH_ACCOUNT_ID`.
- **Portfolio Valuation**: Patched onto `BB_TR_WEALTH_ACCOUNT_ID` via `PATCH /v1/api/accounts` so `currentBalance` equals live TR security portfolio market value.

## 4. Non-Functional Requirements
- **Zero Secrets**: No credentials or personal UUIDs in git history.
- **Mock Mode**: Full support for `USE_MOCK_DATA=true` and `DRY_RUN=true` for offline testing and CI verification.
- **Polling Intervals**: IBKR every 6 hours (`21600s`), Trade Republic every 15 minutes (`900s`).
