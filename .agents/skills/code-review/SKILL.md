---
name: code-review
description: >-
  Audit code changes against AGENTS.md invariants: zero secrets, strict mypy typing,
  ruff lint/security rules, BudgetBakers batch limits, and test coverage.
---

# `/code-review` — Codebase Health & Security Review Skill

When reviewing changes in `finance-sync`, verify every item below:
1. **Zero Secrets**: Are any tokens, PINs, phone numbers, Discord IDs, or real personal account UUIDs present in tracked files?
2. **Type Safety**: Does `uv run mypy .` pass with `strict = true` and zero `Any` leaks?
3. **Lint & Security**: Does `uv run ruff check .` (including `S` bandit security rules and `ANN` annotations) pass cleanly?
4. **API & DB Invariants**:
   - Are `POST /v1/api/records` calls chunked to `<=20` items?
   - Does `PATCH /v1/api/accounts` compute `initialBalance = round(target - totalIncomes + totalExpenses, 2)`?
   - Do stock buy records include shares, ISIN, and price in `note`?
   - Does SQLite use WAL mode and `sha256(broker + external_id + timestamp + f"{float(amount):.2f}")`?
5. **Tests**: Do unit, integration, and E2E tests cover both happy paths and error/retry edge cases?
