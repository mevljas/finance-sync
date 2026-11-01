---
name: to-tickets
description: >-
  Break a technical specification down into independent vertical-slice (tracer-bullet)
  implementation tasks that each deliver testable value.
---

# `/to-tickets` — Vertical-Slice Task Breakdown Skill

Break a specification into ordered, self-contained vertical slices:
1. **Slice 1**: Configuration (`config/settings.py`) & SQLite SHA-256 Store (`shared/database.py`) + unit tests.
2. **Slice 2**: BudgetBakers REST Client (`shared/budgetbakers.py`) + unit tests (`tests/test_budgetbakers.py`).
3. **Slice 3**: IBKR Flex Client & Worker (`ibkr_worker/`) + XML fixture + unit & integration tests (`tests/test_ibkr_parser.py`, `tests/integration/test_ibkr_sync.py`).
4. **Slice 4**: Trade Republic Session Manager, Discord 2FA Bot, Event Parser & Worker (`tr_worker/`) + JSON fixtures + unit & integration tests (`tests/test_tr_parser.py`, `tests/integration/test_tr_sync.py`).
5. **Slice 5**: Mock Data Mode (`shared/mock_data.py`) + E2E Pipeline Tests (`tests/e2e/test_e2e_pipeline.py`).
6. **Slice 6**: Alpine `Dockerfile`, Portainer `docker-compose.yml`, GitHub Actions CI (`.github/workflows/ci.yml`), and `README.md`.
