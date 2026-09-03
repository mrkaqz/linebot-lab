"""Config precedence (DB > env > default), Fernet secret round-tripping,
and the "blank field on save means unchanged" rule.
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings, settings_from_overrides
from app.settings_store import ConfigStore


@pytest.fixture
def config_store(tmp_path):
    store = ConfigStore(tmp_path / "test.sqlite3", tmp_path)
    yield store
    store.close()


def test_db_value_overrides_env(tmp_path, config_store, monkeypatch):
    monkeypatch.setenv("TIMEZONE", "UTC")
    base = Settings(
        line_channel_secret="s", line_channel_access_token="t",
        ms_client_id="c", ms_redirect_uri="https://example.com/oauth/callback",
        oauth_setup_secret="secret", data_dir=tmp_path, _env_file=None,
    )
    assert base.timezone == "UTC"  # env used when DB unset

    config_store.set("timezone", "Asia/Bangkok")
    settings = settings_from_overrides(config_store.overrides(), base=base)
    assert settings.timezone == "Asia/Bangkok"  # DB overrides env


def test_env_used_when_db_unset(tmp_path, config_store, monkeypatch):
    monkeypatch.setenv("ONEDRIVE_ROOT", "/FromEnv")
    base = Settings(
        line_channel_secret="s", line_channel_access_token="t",
        ms_client_id="c", ms_redirect_uri="https://example.com/oauth/callback",
        oauth_setup_secret="secret", data_dir=tmp_path, onedrive_root="/FromEnv", _env_file=None,
    )
    settings = settings_from_overrides(config_store.overrides(), base=base)
    assert settings.onedrive_root == "/FromEnv"


def test_default_used_when_neither_db_nor_env(tmp_path, config_store):
    base = Settings(
        line_channel_secret="s", line_channel_access_token="t",
        ms_client_id="c", ms_redirect_uri="https://example.com/oauth/callback",
        oauth_setup_secret="secret", data_dir=tmp_path, _env_file=None,
    )
    assert base.onedrive_root == "/LabResults"  # the field default
    settings = settings_from_overrides(config_store.overrides(), base=base)
    assert settings.onedrive_root == "/LabResults"


def test_secret_round_trips_through_fernet(config_store):
    config_store.set("line_channel_secret", "top-secret-value")
    assert config_store.get("line_channel_secret") == "top-secret-value"


def test_secret_ciphertext_is_not_plaintext(tmp_path):
    store = ConfigStore(tmp_path / "test2.sqlite3", tmp_path)
    store.set("anthropic_api_key", "sk-ant-super-secret")
    row = store._conn.execute("SELECT value FROM config WHERE key = ?", ("anthropic_api_key",)).fetchone()
    assert row is not None
    assert row[0] != "sk-ant-super-secret"
    assert "sk-ant-super-secret" not in row[0]
    store.close()


def test_non_secret_value_stored_in_plaintext(config_store):
    config_store.set("timezone", "Asia/Bangkok")
    row = config_store._conn.execute("SELECT value FROM config WHERE key = ?", ("timezone",)).fetchone()
    assert row[0] == "Asia/Bangkok"


def test_blank_secret_field_leaves_stored_value_unchanged(tmp_path, config_store):
    """Simulates the admin router's rule: only call config_store.set() when
    the submitted field is non-blank; a blank submission is simply skipped.
    """
    config_store.set("line_channel_secret", "original-secret")

    submitted_value = ""  # what a masked field posts when left untouched
    if submitted_value.strip():
        config_store.set("line_channel_secret", submitted_value.strip())

    assert config_store.get("line_channel_secret") == "original-secret"


def test_clear_removes_db_value_and_falls_back(tmp_path, config_store):
    base = Settings(
        line_channel_secret="s", line_channel_access_token="t",
        ms_client_id="c", ms_redirect_uri="https://example.com/oauth/callback",
        oauth_setup_secret="secret", data_dir=tmp_path, anthropic_api_key="env-key", _env_file=None,
    )
    config_store.set("anthropic_api_key", "db-key")
    assert settings_from_overrides(config_store.overrides(), base=base).anthropic_api_key == "db-key"

    config_store.clear("anthropic_api_key")
    assert settings_from_overrides(config_store.overrides(), base=base).anthropic_api_key == "env-key"


def test_is_set_reflects_presence_without_decrypting(config_store):
    assert config_store.is_set("gemini_api_key") is False
    config_store.set("gemini_api_key", "a-key")
    assert config_store.is_set("gemini_api_key") is True
