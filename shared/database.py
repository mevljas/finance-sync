"""SQLite deduplication store tracking seen records via SHA-256 hashes."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SeenRecordEntry:
    """Represents a single deduplicated transaction entry in SQLite."""

    record_hash: str
    broker: str
    timestamp: str


def compute_record_hash(
    broker: str,
    external_id: str,
    timestamp: str,
    amount: float | int | Decimal | str,
) -> str:
    """Compute deterministic SHA-256 hash: sha256(broker + external_id + timestamp + amount)."""
    normalized_amount = f"{float(amount):.2f}"
    raw = f"{broker}{external_id}{timestamp}{normalized_amount}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DeduplicationStore:
    """SQLite-backed deduplication and sync metadata store using WAL mode."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_records (
                    hash TEXT PRIMARY KEY,
                    broker TEXT NOT NULL,
                    timestamp DATETIME NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_seen_records_broker
                ON seen_records (broker);
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def is_seen(self, record_hash: str) -> bool:
        """Return True if the given SHA-256 hash already exists in seen_records."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM seen_records WHERE hash = ? LIMIT 1;",
                (record_hash,),
            )
            return cursor.fetchone() is not None

    def mark_seen(self, record_hash: str, broker: str, timestamp: str) -> None:
        """Insert a single record hash into seen_records (idempotent)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO seen_records (hash, broker, timestamp)
                VALUES (?, ?, ?);
                """,
                (record_hash, broker, timestamp),
            )

    def mark_seen_batch(self, entries: Iterable[SeenRecordEntry]) -> int:
        """Insert a batch of SeenRecordEntry items into seen_records."""
        rows = [(e.record_hash, e.broker, e.timestamp) for e in entries]
        if not rows:
            return 0
        with self._connect() as conn:
            cursor = conn.executemany(
                """
                INSERT OR IGNORE INTO seen_records (hash, broker, timestamp)
                VALUES (?, ?, ?);
                """,
                rows,
            )
            return cursor.rowcount if cursor.rowcount >= 0 else len(rows)

    def get_seen_hashes(self, broker: str | None = None) -> set[str]:
        """Return all stored hashes, optionally filtered by broker."""
        with self._connect() as conn:
            if broker is not None:
                cursor = conn.execute(
                    "SELECT hash FROM seen_records WHERE broker = ?;",
                    (broker,),
                )
            else:
                cursor = conn.execute("SELECT hash FROM seen_records;")
            return {str(row[0]) for row in cursor.fetchall()}

    def count(self, broker: str | None = None) -> int:
        """Return the number of seen records in the database."""
        with self._connect() as conn:
            if broker is not None:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM seen_records WHERE broker = ?;",
                    (broker,),
                )
            else:
                cursor = conn.execute("SELECT COUNT(*) FROM seen_records;")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def get_metadata(self, key: str) -> str | None:
        """Retrieve a value from sync_metadata by key."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT value FROM sync_metadata WHERE key = ? LIMIT 1;",
                (key,),
            )
            row = cursor.fetchone()
            return str(row[0]) if row is not None else None

    def set_metadata(self, key: str, value: str) -> None:
        """Upsert a key-value pair in sync_metadata."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_metadata (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """,
                (key, value),
            )
