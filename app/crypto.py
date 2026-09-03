"""At-rest encryption for secrets stored in the config DB, and password
hashing for the admin UI login.

**Threat model, stated honestly**: the Fernet key lives in a file right next
to the SQLite database it protects (`data/secret.key`, mode 0600). This
protects a leaked/copied *database file* (a backup pulled off the Pi, a
stolen SD card image mounted elsewhere without the accompanying key file
being copied too, a bug that emails the wrong attachment) -- the secrets in
it are ciphertext without the key. It does **not** protect against anyone
who has access to the Pi's filesystem while both files are present (e.g. a
`docker compose exec` shell, a full disk image, root on the host) -- they
can read the key file just as easily as the database. This is
symmetric-at-rest-with-local-key encryption, not a secrets vault; treat the
Pi itself as the trust boundary, not this encryption.

Password hashing uses stdlib `hashlib.scrypt` (no bcrypt/argon2 dependency)
with a random salt generated fresh for every password set.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_KEY_FILENAME = "secret.key"
_SESSION_KEY_FILENAME = "session_secret.key"

# scrypt cost parameters. n=2**14 is the interactive/login-form-appropriate
# cost recommended by NIST/OWASP guidance circa the 2020s; comfortably fast
# (well under 100ms) on a Raspberry Pi 4, while still expensive to brute
# force offline.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _write_private_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def load_or_create_fernet_key(data_dir: Path) -> bytes:
    """Return the install's Fernet key, generating and persisting one
    (mode 0600) on first use."""
    path = data_dir / _KEY_FILENAME
    if path.exists():
        return path.read_bytes()
    key = Fernet.generate_key()
    _write_private_file(path, key)
    return key


def load_or_create_session_secret(data_dir: Path) -> str:
    """Return the install's session-cookie signing secret, generating and
    persisting one (mode 0600) on first use, so sessions survive restarts."""
    path = data_dir / _SESSION_KEY_FILENAME
    if path.exists():
        return path.read_text(encoding="utf-8")
    secret = secrets.token_urlsafe(64)
    _write_private_file(path, secret.encode("utf-8"))
    return secret


class SecretBox:
    """Thin wrapper around Fernet for encrypting/decrypting config values."""

    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Stored secret could not be decrypted (wrong/rotated key?).") from exc


def hash_password(password: str) -> str:
    """Hash `password` with scrypt + a fresh random salt. Returns a single
    self-describing string: 'scrypt$N$r$p$saltHex$hashHex'."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verify `password` against a hash produced by `hash_password`. Returns
    False (never raises) for a malformed/foreign hash string."""
    try:
        scheme, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
    return hmac.compare_digest(candidate, expected)
