---
name: grill-with-docs
description: >-
  Stateful architectural interview skill that interrogates a design against docs/GLOSSARY.md
  and docs/adr/, writing new ADRs and updating ubiquitous language as decisions are made.
---

# `/grill-with-docs` — Documentation-Driven Design Interrogation

Interrogate a proposed feature or architectural change while keeping `docs/GLOSSARY.md`, `docs/ARCHITECTURE.md`, and `docs/adr/` in lockstep.

## Workflow
1. Read `docs/GLOSSARY.md`, `docs/ARCHITECTURE.md`, and all files in `docs/adr/`.
2. Identify any domain terms, module boundaries, or API invariants impacted by the user's request.
3. Interview the user one question at a time with a `(Recommended)` option.
4. For every architectural decision reached, create or update an ADR in `docs/adr/NNNN-title.md` and add new domain terms to `docs/GLOSSARY.md`.
