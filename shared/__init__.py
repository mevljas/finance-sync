"""Shared utilities for BudgetBakers API client, SQLite deduplication, and mock fixtures."""

from shared.budgetbakers import (
    MAX_DELETE_BATCH_SIZE,
    MAX_RECORDS_BATCH_SIZE,
    AmountPayload,
    BudgetBakersAPIError,
    BudgetBakersClient,
    CreateRecordPayload,
    format_stock_buy_note,
)
from shared.database import DeduplicationStore, SeenRecordEntry, compute_record_hash

__all__ = [
    "MAX_DELETE_BATCH_SIZE",
    "MAX_RECORDS_BATCH_SIZE",
    "AmountPayload",
    "BudgetBakersAPIError",
    "BudgetBakersClient",
    "CreateRecordPayload",
    "DeduplicationStore",
    "SeenRecordEntry",
    "compute_record_hash",
    "format_stock_buy_note",
]
