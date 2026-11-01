---
name: grill-me
description: >-
  Relentlessly interview the user about every aspect of their plan or feature design
  until a shared understanding is reached. Inspired by Matt Pocock (aihero.dev).
---

# `/grill-me` — Design Tree Interrogation Skill

Interview the user about every aspect of their plan until you reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one.

## Rules
1. **Consult Repo Context First**: Always read `AGENTS.md`, `docs/PRD.md`, `docs/ARCHITECTURE.md`, `docs/GLOSSARY.md`, and `docs/adr/` before asking a question. If a question can be answered by exploring the codebase or docs, explore the codebase instead.
2. **One Question at a Time**: Never overwhelm the user with a wall of questions. Ask one focused question at a time.
3. **Provide a Recommended Answer**: For every question, provide your recommended option first, prefixed with `(Recommended)`, explaining the trade-off concisely.
4. **Update Context**: Once all branches of the design tree are resolved, record durable decisions in `docs/PRD.md` or `docs/adr/`.
