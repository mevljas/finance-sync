# AGENTS.md — AI-Agent-Native Engineering Guide (`finance-sync`)

Welcome, agent! This repository is built to be **AI-agent-native** following the [AI Hero (`aihero.dev`) / Matt Pocock skills workflow](https://www.aihero.dev/).
All domain context, architectural invariants, and engineering guardrails are stored directly in the repository so you can operate autonomously with full context.

---

## 1. Repository Context & Documentation Map

Before modifying code, consult these source-of-truth documents in `docs/`:
- **[`docs/PRD.md`](./docs/PRD.md)**: Product Requirements Document, user stories, account mappings, and scope.
- **[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md)**: System design, module boundaries, API protocols, and state machines.
- **[`docs/GLOSSARY.md`](./docs/GLOSSARY.md)**: Ubiquitous domain language (`FlexStatement`, `OpenPosition`, `TimelineEvent`, `Saveback`, `SeenRecordHash`, `BalanceFormula`).
- **[`docs/adr/`](./docs/adr/)**: Architecture Decision Records:
  - [`0001-sqlite-wal-sha256-deduplication.md`](./docs/adr/0001-sqlite-wal-sha256-deduplication.md)
  - [`0002-budgetbakers-initial-balance-patching.md`](./docs/adr/0002-budgetbakers-initial-balance-patching.md)
  - [`0003-discord-bot-2fa-renewal.md`](./docs/adr/0003-discord-bot-2fa-renewal.md)
  - [`0004-gitleaks-secret-scanning-and-test-fixtures.md`](./docs/adr/0004-gitleaks-secret-scanning-and-test-fixtures.md)
  - [`0005-read-only-dry-run-mode.md`](./docs/adr/0005-read-only-dry-run-mode.md)

---

## 2. Hard Invariants & Guardrails

1. **Zero Secrets in Repository**:
   - NEVER commit tokens, PINs, phone numbers, Discord IDs, or real user account UUIDs into source code, tests, docs, or `.env.example`.
   - Use low-entropy zero-padded synthetic IDs in tests (e.g., `"000000000000000001"`, `"00000000-0000-0000-0000-000000000001"`, `"U00000000"`) so `gitleaks` entropy rules are never triggered by dummy strings.
   - Use `SecretStr` in `config/settings.py` for sensitive values so secrets are masked in logs/tracebacks.
2. **Package & Environment Management (`uv`)**:
   - Always use `uv` (`pyproject.toml` + `uv.lock`). Do not use raw `pip` or `requirements.txt`.
3. **Strict Type Hints, Linting & Formatting**:
   - Every function, method, and variable must be strictly typed (`mypy --strict`).
   - All code must pass `uv run ruff format --check .` and `uv run ruff check .` with zero warnings.
4. **BudgetBakers API Contract**:
   - Base URL: `https://rest.budgetbakers.com/wallet`
   - `POST /v1/api/records` accepts a JSON array of at most **20 items per request**. Always chunk larger batches into slices of `<=20`.
   - `DELETE /v1/api/records` accepts `{"ids": [...]}` with at most **10 IDs per request**.
   - `PATCH /v1/api/accounts` accepts `[{"id": "<uuid>", "initialBalance": <float>}]`. Because BudgetBakers computes `currentBalance = initial + totalIncomes - totalExpenses`, always compute:
     `initialBalance = round(target_portfolio_value - totalIncomes + totalExpenses, 2)`
     after creating records so `currentBalance` matches the live broker portfolio value.
   - Stock buy records must embed executed shares, ISIN, and price into the `note` field (`BUY <shares> sh <ISIN> (<symbol>) @ <price> <currency>`).
5. **Deduplication Contract**:
   - SQLite table: `seen_records (hash TEXT PRIMARY KEY, broker TEXT, timestamp DATETIME)`
   - Hash formula: `sha256(broker + external_id + timestamp + f"{float(amount):.2f}")`
6. **Test Coverage & Mock Data Support**:
   - Every change must follow Red-Green-Refactor TDD (`.agents/skills/tdd/SKILL.md`) and be covered by Unit (`tests/test_*.py`), Integration (`tests/integration/`), and E2E (`tests/e2e/`) tests.
   - `USE_MOCK_DATA=true` must work out-of-the-box using synthetic fixtures in `tests/fixtures/`.

---

## 3. Standard Agent Commands

```bash
# Install / sync all dependencies (including dev group)
uv sync --all-groups

# Format check & auto-format
uv run ruff format --check .
uv run ruff format .

# Lint (includes flake8-annotations ANN & bandit security S)
uv run ruff check .

# Strict static type checking
uv run mypy .

# Run full test suite (Unit + Integration + E2E) with coverage
uv run pytest --cov=config --cov=shared --cov=ibkr_worker --cov=tr_worker -v

# Run workers locally in mock/dry-run mode
USE_MOCK_DATA=true uv run python -m ibkr_worker.worker --once --dry-run
USE_MOCK_DATA=true uv run python -m tr_worker.worker --once --dry-run
USE_MOCK_DATA=true uv run python -m ibkr_worker.worker --once --dry-run --dry-run-output ibkr_preview.json
USE_MOCK_DATA=true uv run python -m tr_worker.worker --once --dry-run --dry-run-output tr_preview.json
```

---

## 4. Agent Skills (`.agents/skills/` — Matt Pocock / `aihero.dev`)

This repository includes modular agent skills in `.agents/skills/`:
- **[`grill-me`](./.agents/skills/grill-me/SKILL.md)**: Interview the user relentlessly across every branch of the design tree before writing code.
- **[`grill-with-docs`](./.agents/skills/grill-with-docs/SKILL.md)**: Interrogate a design against `docs/GLOSSARY.md` and `docs/adr/`, updating ADRs as decisions are resolved.
- **[`write-a-prd`](./.agents/skills/write-a-prd/SKILL.md)**: Author or update `docs/PRD.md` with clear module boundaries and acceptance criteria.
- **[`to-spec`](./.agents/skills/to-spec/SKILL.md)**: Transform requirements into technical specifications in `docs/`.
- **[`to-tickets`](./.agents/skills/to-tickets/SKILL.md)**: Break specs into vertical-slice tracer-bullet implementation tasks.
- **[`tdd`](./.agents/skills/tdd/SKILL.md)**: Enforce strict Red-Green-Refactor TDD loops using `uv run pytest`.
- **[`code-review`](./.agents/skills/code-review/SKILL.md)**: Audit diffs for type safety, security (`S` rules), test coverage, and architectural alignment.
- **[`retro`](./.agents/skills/retro/SKILL.md)**: Capture lessons learned after shipping and update `AGENTS.md` / `docs/adr/`.
- **[`handoff`](./.agents/skills/handoff/SKILL.md)**: Compact session state into a clean handoff summary for the next agent session.
