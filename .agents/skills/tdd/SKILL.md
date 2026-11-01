---
name: tdd
description: >-
  Enforce strict Red-Green-Refactor Test-Driven Development (TDD) discipline using
  uv run pytest, ruff, and mypy. Inspired by Matt Pocock (aihero.dev).
---

# `/tdd` — Strict Red-Green-Refactor TDD Skill

Every feature or bugfix in `finance-sync` must follow the **Red -> Green -> Refactor** loop:

## 1. Red (Write a Failing Test First)
- Add or update a test in `tests/` (unit, integration, or e2e) that specifies the exact behavior.
- Run `uv run pytest` and confirm the test fails for the expected reason.

## 2. Green (Minimal Passing Implementation)
- Write the cleanest, strictly typed code to make the test pass.
- Run `uv run pytest` and verify all tests pass.

## 3. Refactor & Quality Gates
- Refactor duplication while keeping tests green.
- Run the full quality suite before completing the task:
  ```bash
  uv run ruff format --check .
  uv run ruff check .
  uv run mypy .
  uv run pytest --cov=config --cov=shared --cov=ibkr_worker --cov=tr_worker -v
  ```
