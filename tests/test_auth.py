"""Password hashing/verification (stdlib scrypt, no bcrypt/argon2), and the
first-boot random-password generation flow.
"""

from __future__ import annotations

import pytest

from app.auth import check_admin_password, ensure_admin_password, set_admin_password
from app.crypto import hash_password, verify_password
from app.settings_store import ADMIN_PASSWORD_HASH_KEY, ConfigStore


def test_verify_correct_password():
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", hashed) is True


def test_verify_wrong_password():
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("wrong-password", hashed) is False


def test_hash_is_salted_differently_each_time():
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a) is True
    assert verify_password("same-password", b) is True


def test_verify_rejects_malformed_hash():
    assert verify_password("anything", "not-a-real-hash") is False


def test_hash_scheme_is_scrypt_stdlib_only():
    hashed = hash_password("x")
    assert hashed.startswith("scrypt$")


@pytest.fixture
def config_store(tmp_path):
    store = ConfigStore(tmp_path / "auth.sqlite3", tmp_path)
    yield store
    store.close()


def test_ensure_admin_password_generates_on_first_boot(config_store):
    assert config_store.is_set(ADMIN_PASSWORD_HASH_KEY) is False
    plaintext = ensure_admin_password(config_store)
    assert plaintext is not None
    assert len(plaintext) >= 16
    assert check_admin_password(config_store, plaintext) is True


def test_ensure_admin_password_noop_if_already_set(config_store):
    set_admin_password(config_store, "my-chosen-password")
    result = ensure_admin_password(config_store)
    assert result is None
    assert check_admin_password(config_store, "my-chosen-password") is True


def test_check_admin_password_wrong_password_fails(config_store):
    set_admin_password(config_store, "the-real-password")
    assert check_admin_password(config_store, "not-it") is False


def test_check_admin_password_with_no_password_set_fails(config_store):
    assert check_admin_password(config_store, "anything") is False
