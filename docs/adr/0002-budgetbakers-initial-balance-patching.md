# ADR 0002: BudgetBakers Investment Account Initial Balance Patching

## Status
Accepted

## Context
In BudgetBakers Wallet, every standard and investment account computes its live balance using the read-only formula:
`currentBalance = initial + totalIncomes - totalExpenses`.
When we log stock buys (ETF/stock purchases on IBKR or savings plans on Trade Republic) as `expense` records (so that executed shares, ISIN, and execution price are recorded in `note`), `totalExpenses` increases, which would otherwise subtract the purchase cost from `currentBalance`. Furthermore, market price fluctuations in `OpenPositions` / portfolio valuation must be reflected in the account's `currentBalance`.

## Decision
After pushing new records to an investment account (`BB_IBKR_ACCOUNT_ID` or `BB_TR_WEALTH_ACCOUNT_ID`), the client:
1. Fetches `GET /v1/api/accounts?id={account_id}` to read the latest `recordStats.totalIncomes` and `recordStats.totalExpenses`.
2. Computes `initialBalance = round(target_portfolio_value - totalIncomes + totalExpenses, 2)`.
3. Sends `PATCH /v1/api/accounts` with `[{"id": account_id, "initialBalance": initialBalance}]`.

## Consequences
- Stock buy records are preserved with full share/ISIN/price auditability in Wallet.
- The investment account's `currentBalance` in Wallet always equals the exact live broker portfolio market value.
