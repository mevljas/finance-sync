---
name: to-spec
description: >-
  Convert shaped requirements from docs/PRD.md into a concrete technical specification
  detailing schemas, function signatures, and test cases.
---

# `/to-spec` — Technical Specification Skill

Translate product requirements in `docs/PRD.md` into an actionable technical specification in `docs/ARCHITECTURE.md`.

## Checklist
- Define strict Pydantic models and Python type signatures (`mypy --strict`).
- Specify external HTTP contracts (BudgetBakers `/v1/api/records` batch limit `<=20`, `/v1/api/accounts` `initialBalance` formula, IBKR Flex 2-step XML, TR WebSocket & Discord v10 API).
- Specify SQLite schema and SHA-256 hash invariants.
- Enumerate Unit, Integration, and E2E test scenarios.
