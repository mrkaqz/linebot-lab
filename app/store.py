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
"""


class Store:
    """Thin wrapper around a SQLite database file."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

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

    def close(self) -> None:
        self._conn.close()
