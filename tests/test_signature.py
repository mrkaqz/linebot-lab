"""Webhook signature verification: valid body, tampered body, wrong secret."""

from __future__ import annotations

import base64
import hashlib
import hmac

from app.line_client import verify_signature


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def test_valid_signature_passes():
    secret = "s3cr3t"
    body = b'{"events": []}'
    signature = _sign(secret, body)
    assert verify_signature(secret, body, signature) is True


def test_tampered_body_fails():
    secret = "s3cr3t"
    body = b'{"events": []}'
    signature = _sign(secret, body)
    tampered_body = b'{"events": [{"injected": true}]}'
    assert verify_signature(secret, tampered_body, signature) is False


def test_wrong_secret_fails():
    body = b'{"events": []}'
    signature = _sign("correct-secret", body)
    assert verify_signature("wrong-secret", body, signature) is False


def test_missing_signature_fails():
    assert verify_signature("s3cr3t", b'{"events": []}', "") is False
