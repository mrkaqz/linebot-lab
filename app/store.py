"""SQLite-backed state: processed LINE message ids (idempotency) and a log
of results that could not be filed under an OPD number ("unfiled").

A single `Store` instance is created at startup against
`settings.data_dir / "linebot_lab.sqlite3"` and reused for the life of the
process. All methods are synchronous (sqlite3 is fast enough for this
workload, and the pipeline is single-consumer), but pipeline code calls them
via `asyncio.to_thread` so a slow disk never blocks the event loop.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    message_id TEXT PRIMARY KEY,
    processed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS unfiled (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    received_at REAL NOT NULL,
    jpg_path TEXT,
    md_path TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    opd_number TEXT,
    detail TEXT
);
"""

# Columns added after the initial release. Applied with ALTER TABLE ADD
# COLUMN (idempotent, guarded by PRAGMA table_info) so an existing
# deployment's database upgrades in place instead of needing a fresh DB.
_UNFILED_MIGRATIONS = [
    ("resolved", "INTEGER NOT NULL DEFAULT 0"),
    ("resolved_at", "REAL"),
    ("resolution", "TEXT"),  # 'filed' | 'dismissed'
]


class Store:
    """Thin wrapper around a SQLite database file."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._migrate_unfiled_table()
        self._conn.commit()

    def _migrate_unfiled_table(self) -> None:
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(unfiled)")}
        for column, coltype in _UNFILED_MIGRATIONS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE unfiled ADD COLUMN {column} {coltype}")

    def mark_processed(self, message_id: str) -> bool:
        """Record `message_id` as processed. Returns True if this is the
        first time it has been seen (i.e. work should proceed), False if it
        was already recorded (a LINE webhook retry -- skip).
        """
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO processed (message_id, processed_at) VALUES (?, ?)",
            (message_id, time.time()),
        )
        self._conn.commit()
        is_new = cur.rowcount == 1
        if not is_new:
            logger.info("messageId %s already processed; skipping (LINE retry)", message_id)
        return is_new

    def record_unfiled(
        self,
        message_id: str,
        received_at: float,
        jpg_path: Optional[str],
        md_path: Optional[str],
        reason: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO unfiled (message_id, received_at, jpg_path, md_path, reason) VALUES (?, ?, ?, ?, ?)",
            (message_id, received_at, jpg_path, md_path, reason),
        )
        self._conn.commit()

    def get_unfiled(self, row_id: int) -> Optional[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        row = self._conn.execute("SELECT * FROM unfiled WHERE id = ?", (row_id,)).fetchone()
        self._conn.row_factory = None
        return row

    def list_unfiled(self, *, resolved: Optional[bool] = False, limit: int = 200) -> list[sqlite3.Row]:
        """List rows from `unfiled`, newest first. `resolved=False` (the
        default) is the queue the admin UI shows; pass None for all rows."""
        self._conn.row_factory = sqlite3.Row
        if resolved is None:
            rows = self._conn.execute(
                "SELECT * FROM unfiled ORDER BY received_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM unfiled WHERE resolved = ? ORDER BY received_at DESC LIMIT ?",
                (1 if resolved else 0, limit),
            ).fetchall()
        self._conn.row_factory = None
        return rows

    def count_unfiled_unresolved(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM unfiled WHERE resolved = 0").fetchone()
        return row[0]

    def resolve_unfiled(
        self,
        row_id: int,
        resolution: str,
        *,
        new_jpg_path: Optional[str] = None,
        new_md_path: Optional[str] = None,
    ) -> None:
        """Mark an `unfiled` row resolved. Only call this AFTER any OneDrive
        move has actually succeeded -- never optimistically."""
        if new_jpg_path is not None or new_md_path is not None:
            self._conn.execute(
                "UPDATE unfiled SET resolved = 1, resolved_at = ?, resolution = ?, "
                "jpg_path = COALESCE(?, jpg_path), md_path = COALESCE(?, md_path) WHERE id = ?",
                (time.time(), resolution, new_jpg_path, new_md_path, row_id),
            )
        else:
            self._conn.execute(
                "UPDATE unfiled SET resolved = 1, resolved_at = ?, resolution = ? WHERE id = ?",
                (time.time(), resolution, row_id),
            )
        self._conn.commit()

    def record_activity(self, kind: str, opd_number: Optional[str], detail: str = "") -> None:
        """Append one row to the `activity` log (dashboard recent-activity
        list). `kind` is a short free-form label, e.g. 'filed', 'unfiled',
        'resolved', 'dismissed'."""
        self._conn.execute(
            "INSERT INTO activity (ts, kind, opd_number, detail) VALUES (?, ?, ?, ?)",
            (time.time(), kind, opd_number, detail),
        )
        self._conn.commit()

    def recent_activity(self, limit: int = 20) -> list[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        rows = self._conn.execute("SELECT * FROM activity ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        self._conn.row_factory = None
        return rows

    def last_filed(self) -> Optional[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        row = self._conn.execute(
            "SELECT * FROM activity WHERE kind = 'filed' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        self._conn.row_factory = None
        return row

    def count_filed_since(self, since_epoch: float) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM activity WHERE kind = 'filed' AND ts >= ?", (since_epoch,)
        ).fetchone()
        return row[0]

    def close(self) -> None:
        self._conn.close()
