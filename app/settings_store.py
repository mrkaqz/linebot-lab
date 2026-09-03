"""DB-backed configuration, layered on top of (and falling back to)
environment variables.

Precedence, field by field: **DB value (set via the admin UI) > environment
variable / `.env` > pydantic field default.** This is achieved cheaply: a
`ConfigStore.overrides()` dict is passed as explicit keyword arguments to
`Settings(...)`, and pydantic-settings' default source order already puts
init kwargs ahead of the environment -- see `app/config.py`. A field simply
absent from the DB falls straight through to the existing env/default
behaviour untouched, so a deployment that only ever used `.env` keeps
working exactly as before.

`LINE_CHANNEL_SECRET`, `LINE_CHANNEL_ACCESS_TOKEN`, `ANTHROPIC_API_KEY` and
`GEMINI_API_KEY` are encrypted at rest with `app.crypto.SecretBox` before
being written to the `config` table -- see that module's docstring for what
this does and does not protect against.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

from .crypto import SecretBox, load_or_create_fernet_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL NOT NULL
);
"""

# Settings fields that are encrypted at rest. Field names here match
# app.config.Settings attribute names (lower_snake_case).
SECRET_FIELDS: frozenset[str] = frozenset(
    {"line_channel_secret", "line_channel_access_token", "anthropic_api_key", "gemini_api_key"}
)

# Settings fields the admin UI is allowed to persist to the DB. Anything not
# in this set (e.g. ms_client_id, oauth_setup_secret, data_dir) stays
# env-only by design -- see README "Web admin UI".
OVERRIDABLE_SETTINGS_FIELDS: frozenset[str] = frozenset(
    {
        "line_channel_secret",
        "line_channel_access_token",
        "line_lab_group_id",
        "admin_line_id",
        "ocr_backend",
        "claude_model",
        "anthropic_api_key",
        "gemini_model",
        "gemini_api_key",
        "opd_regex",
        "timezone",
        "onedrive_root",
        "onedrive_folder_id",
        "onedrive_folder_path",
        "setup_ui_exposure",
        "log_level",
    }
)

# Non-Settings admin-only keys, stored the same way (not surfaced through
# `overrides()`/Settings at all).
ADMIN_PASSWORD_HASH_KEY = "admin_password_hash"
LINE_GROUP_CANDIDATE_NAME_KEY = "line_lab_group_candidate_name"


class ConfigStore:
    """SQLite-backed key/value config store, in the same database file as
    `app.store.Store` (a separate `sqlite3.connect()` to that file -- fine
    for this app's single-Pi, low-concurrency write pattern).
    """

    def __init__(self, db_path: Path, data_dir: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._box = SecretBox(load_or_create_fernet_key(data_dir))

    def close(self) -> None:
        self._conn.close()

    # ---- raw key/value access ----

    def get(self, key: str) -> Optional[str]:
        """Return the stored, decrypted value for `key`, or None if unset."""
        row = self._conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        if row is None or row[0] is None:
            return None
        value = row[0]
        if key in SECRET_FIELDS:
            return self._box.decrypt(value)
        return value

    def is_set(self, key: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM config WHERE key = ? AND value IS NOT NULL", (key,)).fetchone()
        return row is not None

    def set(self, key: str, value: str) -> None:
        stored = self._box.encrypt(value) if key in SECRET_FIELDS else value
        self._conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, stored, time.time()),
        )
        self._conn.commit()

    def clear(self, key: str) -> None:
        """Remove `key` entirely, so Settings falls back to env/default."""
        self._conn.execute("DELETE FROM config WHERE key = ?", (key,))
        self._conn.commit()

    # ---- Settings integration ----

    def overrides(self) -> dict[str, str]:
        """Return {field_name: decrypted_value} for every overridable
        Settings field that has a DB value, suitable for
        `Settings(**overrides)`. Fields not set in the DB are simply
        absent, so pydantic-settings falls through to env/default for them.
        """
        result: dict[str, str] = {}
        rows = self._conn.execute("SELECT key, value FROM config WHERE key IN ({})".format(
            ",".join("?" * len(OVERRIDABLE_SETTINGS_FIELDS))
        ), tuple(OVERRIDABLE_SETTINGS_FIELDS)).fetchall()
        for key, value in rows:
            if value is None:
                continue
            result[key] = self._box.decrypt(value) if key in SECRET_FIELDS else value
        return result
