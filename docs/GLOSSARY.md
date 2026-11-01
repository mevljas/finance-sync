# Ubiquitous Domain Glossary (`docs/GLOSSARY.md`)

| Term | Definition |
| :--- | :--- |
| **Activity Flex Query** | An XML statement configured in Interactive Brokers Account Management containing `Trades`, `CashTransactions`, `OpenPositions`, and `CashReport` sections. |
| **Two-Step Flex Polling** | The IBKR Flex Web Service protocol: Step 1 calls `SendRequest` to obtain a `<ReferenceCode>` and `<Url>`; Step 2 waits 10 seconds and calls `GetStatement` with the reference code to download the generated XML. |
| **Stock Buy Note** | The standardized BudgetBakers `note` format required for stock/ETF purchases, embedding executed shares, ISIN, symbol/name, and execution price: `BUY <shares> sh <ISIN> (<symbol>) @ <price> <currency>`. |
| **Balance Formula** | BudgetBakers Wallet's invariant for standard/investment accounts: `currentBalance = initial + totalIncomes - totalExpenses`. Updating an account's live market value requires patching `initialBalance = target_value - totalIncomes + totalExpenses`. |
| **Saveback** | Trade Republic's 1% card cashback reward program that automatically invests earned bonus income into a selected savings plan security. Modeled as bonus `income` (`Interest, dividends`) on `BB_TR_WEALTH_ACCOUNT_ID`. |
| **Savings Plan (`Sparplan`)** | Recurring automated ETF/stock investment in Trade Republic. Deducts cash (`expense` on `BB_TR_CASH_ACCOUNT_ID`) and logs the security purchase with shares, ISIN, and price (`expense` on `BB_TR_WEALTH_ACCOUNT_ID`). |
| **Seen Record Hash** | The deterministic SHA-256 primary key stored in SQLite `seen_records`: `sha256(broker + external_id + timestamp + f"{float(amount):.2f}")`. |
| **Backfill Reconciliation** | The initial one-time startup routine in `tr_worker` that removes legacy manual `-50 EUR` `"Savings plan"` records on `Trade republic` cash account and matches existing manual `Interest` entries (`±3 days` + exact amount) so full history can be backfilled without duplicates. |
| **Low-Entropy Synthetic Fixture** | Zero-padded dummy identifiers (`"000000000000000001"`, `"U00000000"`) used in tests and `.env.example` so secret scanners (`gitleaks`) never flag test data for high Shannon entropy (see ADR-0004). |
