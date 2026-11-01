# ADR 0004: Gitleaks Secret Scanning and Low-Entropy Synthetic Test Fixtures

## Status
Accepted

## Context
The repository enforces a strict Zero-Secrets Policy in GitHub Actions via `gitleaks/gitleaks-action@v2`. During initial CI verification, Gitleaks flagged two false positives:
1. `discord-client-id` on an 18-digit ascending dummy string (`"123456789012345678"`, Shannon entropy `3.281 > 3.0`) in `tests/conftest.py`.
2. `curl-auth-header` on a documentation `curl` example passing a literal placeholder token in `README.md`.

## Decision
1. **Low-Entropy Synthetic Identifiers**: All test fixtures (`tests/conftest.py`, `tests/test_*.py`, `tests/e2e/`) and `.env.example` must use low-entropy zero-padded identifiers (e.g., `"000000000000000001"` for Discord channel IDs, `"00000000-0000-0000-0000-000000000001"` for UUIDs, `"U00000000"` for broker account IDs) whose Shannon entropy stays well below secret-scanner thresholds.
2. **Environment Variable References in Documentation**: Shell snippets in documentation (`README.md`, `docs/`) must reference interpolated environment variables (`${BUDGETBAKERS_TOKEN}`) rather than literal token placeholders.
3. **Tracked `.gitleaks.toml`**: Maintain a repository-root `.gitleaks.toml` extending the default ruleset (`useDefault = true`) with explicit, narrowly-scoped allowlists for `.env.example`, `tests/fixtures/*`, and `${BUDGETBAKERS_TOKEN}` shell interpolation.

## Consequences
- Gitleaks remains active and strict across all commits and pull requests without false-positive build failures on synthetic test fixtures or documentation.
