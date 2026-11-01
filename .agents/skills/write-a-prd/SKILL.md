---
name: write-a-prd
description: >-
  Transform a feature idea into a structured Product Requirements Document in docs/PRD.md
  with clear module boundaries, user flows, and acceptance criteria.
---

# `/write-a-prd` — Product Requirements Document Skill

Transform a feature concept into a concrete Product Requirements Document (`docs/PRD.md`).

## Workflow
1. Explore the existing codebase (`config/`, `shared/`, `ibkr_worker/`, `tr_worker/`) and `docs/`.
2. Clarify any ambiguities with the user one question at a time.
3. Write or update `docs/PRD.md` covering:
   - Problem Statement & Goals
   - Account & Category Mapping Behavior
   - Deep Module Boundaries (`shared/budgetbakers.py`, `shared/database.py`, workers)
   - Non-Functional Requirements (Zero secrets, `USE_MOCK_DATA=true`, strict typing)
   - Verification & Acceptance Criteria
