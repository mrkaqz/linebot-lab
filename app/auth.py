"""Admin UI authentication: password hashing (delegated to app.crypto),
session cookie helpers, and simple in-memory login rate limiting.

There is exactly one admin account/password, shared by anyone who reaches
the UI (LAN, or public if SETUP_UI_EXPOSURE=public). This is intentionally
minimal -- a single-operator clinic appliance, not a multi-user system.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException, Request

from .crypto import hash_password, verify_password
from .settings_store import ADMIN_PASSWORD_HASH_KEY, ConfigStore

logger = logging.getLogger(__name__)

MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
SESSION_KEY = "authenticated"


@dataclass
class _Attempt:
    failures: int = 0
    locked_until: float = 0.0


@dataclass
class LoginRateLimiter:
    """In-memory, per-process, keyed by client IP. Resets on restart -- that
    is an acceptable trade for a single-appliance Pi service; it is not
    meant to survive a distributed attack.
    """

    _attempts: dict[str, _Attempt] = field(default_factory=dict)

    def is_locked(self, key: str) -> Optional[float]:
        """Return seconds remaining if `key` is locked out, else None."""
        attempt = self._attempts.get(key)
        if attempt is None:
            return None
        remaining = attempt.locked_until - time.time()
        return remaining if remaining > 0 else None

    def record_failure(self, key: str) -> None:
        attempt = self._attempts.setdefault(key, _Attempt())
        attempt.failures += 1
        if attempt.failures >= MAX_LOGIN_FAILURES:
            attempt.locked_until = time.time() + LOCKOUT_SECONDS
            logger.warning("Admin login locked out for %s after %d failed attempts", key, attempt.failures)

    def record_success(self, key: str) -> None:
        self._attempts.pop(key, None)


def ensure_admin_password(config_store: ConfigStore) -> Optional[str]:
    """If no admin password is set yet, generate one, store its hash, and
    return the PLAINTEXT so the caller can log it once at WARNING. Returns
    None if a password was already set (the normal case after first boot).
    """
    if config_store.is_set(ADMIN_PASSWORD_HASH_KEY):
        return None
    import secrets

    plaintext = secrets.token_urlsafe(16)
    config_store.set(ADMIN_PASSWORD_HASH_KEY, hash_password(plaintext))
    return plaintext


def set_admin_password(config_store: ConfigStore, new_password: str) -> None:
    config_store.set(ADMIN_PASSWORD_HASH_KEY, hash_password(new_password))


def check_admin_password(config_store: ConfigStore, candidate: str) -> bool:
    stored = config_store.get(ADMIN_PASSWORD_HASH_KEY)
    if not stored:
        return False
    return verify_password(candidate, stored)


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def is_logged_in(request: Request) -> bool:
    try:
        return bool(request.session.get(SESSION_KEY))
    except AssertionError:
        # SessionMiddleware not installed on this app -- treat as logged out
        # rather than raising a 500, so misconfiguration fails closed.
        return False


def log_in(request: Request) -> None:
    request.session[SESSION_KEY] = True


def log_out(request: Request) -> None:
    request.session.clear()


def require_login(request: Request) -> None:
    """FastAPI dependency for JSON/API admin routes: 401 if not logged in."""
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="Login required.")


def require_login_page(request: Request) -> None:
    """FastAPI dependency for browser-rendered admin pages: redirects to
    /login instead of a bare 401, since a person -- not a script -- is the
    expected caller.

    (Deliberately a plain module-level function, not a callable class
    instance: with `from __future__ import annotations` in effect here,
    FastAPI resolves a dependency's string type annotations via its
    `__globals__` -- present on a function, absent on a class instance --
    so a callable-object dependency would silently fail to recognize
    `Request` as the special injected type and instead try to parse it as a
    query parameter.)
    """
    if not is_logged_in(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})
