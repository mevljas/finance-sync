# ADR-0014: Trade Republic Card Categorization via `timeline_detail_v2`, MCC, and Normalized Keywords

- **Status**: Accepted
- **Date**: 2026-11-21
- **Deciders**: User, Antigravity Agent
- **Related**: [ADR-0011](./0011-discord-gateway-ready-first-2fa.md)

## Context

Trade Republic card spending events in `timeline_transactions` often omit a top-level `mcc` field and contain merchant names with punctuation or regional terms (for example, `"dm-drogerie"` failing to match `"dm drogerie"`, or `"Restavracija Fresco"` failing to match `"restaurant"`). As a result, card transactions fell back to BudgetBakers Wallet's `Unknown expense` category (`5c5c32c9-0082-8000-8000-000000000000`) and required manual editing.

## Decision

1. **Post-Cutoff Card `timeline_detail_v2` Fetching (`tr_worker/session_manager.py` & `tr_worker/worker.py`)**:
   - `TRSessionManager` accepts `card_cutoff_date` (passed from `settings.tr_card_cutoff_date` in `TRWorker`).
   - `_fetch_ws_data_async()` includes `"card"` in `needs_detail` (recognizing `"timelineDetail"` and `"timelineDetailV2"` actions) for non-canceled/non-pending card events on or after `card_cutoff_date`, avoiding unnecessary WebSocket round trips for older pre-cutoff card payments.
2. **Card Metadata Extraction (`tr_worker/event_parser.py`)**:
   - `_extract_card_category_hints()` inspects top-level fields, nested `timelineDetailV2` sections (`MCC` / `Merchant Category Code` and `Category` / `Kategorie` / `Kategorija` rows), and Trade Republic semantic fallback icon URIs (`logos/merchant-fallback-<slug>/v2`) to extract `(mcc, tr_hint)`.
   - `parse_tr_timeline_events()` passes `(title, mcc=mcc, tr_hint=tr_hint)` to `settings.resolve_merchant_category()`.
3. **Normalized Rules, Expanded BudgetBakers Categories, and Precedence (`config/settings.py`)**:
   - `_normalize_merchant_text()` replaces `-`, `_`, `.`, `/`, and `'` with spaces and collapses whitespace before keyword matching, using space-padded token checks for short keywords (`" dm "`, `" bar "`, `" tus "`, `" mol "`, etc.).
   - Expand BudgetBakers deterministic system category constants (verified against `GET /v1/api/categories`), Slovenian/European merchant keywords, ISO 18245 `MCC_CATEGORY_MAP`, and `TR_HINT_CATEGORY_MAP`.
   - Enforce strict resolution precedence:
     1. Normalized merchant keyword rules (`DEFAULT_MERCHANT_CATEGORY_RULES`)
     2. Card network MCC (`MCC_CATEGORY_MAP`)
     3. Trade Republic `timeline_detail_v2` / icon hint (`TR_HINT_CATEGORY_MAP`)
     4. Fallback (`bb_category_unknown_expense`)

## Consequences

- **Positive**: Future Trade Republic card expenses resolve automatically without requiring manual edits or extra environment variables, while exact merchant keywords override overly generic payment-processor MCCs.
- **Negative / Trade-offs**: Adds one `timeline_detail_v2` WebSocket call per newly fetched post-cutoff card transaction; previously synced `Unknown expense` records in Wallet remain unchanged unless edited manually or purged.
