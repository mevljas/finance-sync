---
name: handoff
description: >-
  Compact the current agent session state, completed slices, open tasks, and verification
  status into a portable markdown summary in docs/HANDOFF.md.
---

# `/handoff` — Session Continuity Skill

When ending a long session or handing off to another agent:
1. Summarize completed work, current git status, and passing test suites (`uv run pytest`).
2. List any remaining tickets or open questions.
3. Reference the relevant ADRs (`docs/adr/`) and architecture sections (`docs/ARCHITECTURE.md`) so the next agent can resume immediately with zero context loss.
