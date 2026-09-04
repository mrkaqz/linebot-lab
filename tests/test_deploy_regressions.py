"""Two regressions found during first deployment.

1. The admin UI's "Sign in to OneDrive" link 404'd. /oauth/start and
   /oauth/callback are registered on the PUBLIC app only (container port
   8000, the one the tunnel forwards); the admin UI is served from a
   different origin (the LAN address on ADMIN_PORT). A relative
   "/oauth/start?..." href therefore resolved against the admin origin,
   where the route does not exist.

2. A rejected LINE webhook logged "invalid signature" and nothing else, so
   an operator staring at LINE's bare "400 Bad Request" had no way to tell a
   stale channel secret from a stripped header from a malformed body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging

from app.line_client import secret_fingerprint
from app.main import build_admin_app, build_public_app

VERIFY_BODY = json.dumps(
    {"destination": "Ua00000000000000000000000000000", "events": []}, separators=(",", ":")
).encode()


def _sign(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


# ------------------------------------------------- oauth start URL (bug 1) --


def test_start_url_is_absolute_and_on_the_public_origin(make_settings):
    settings = make_settings(
        public_base_url="https://linebot-lab.example.dev",
        ms_redirect_uri=None,
        oauth_setup_secret="the-secret",
    )
    assert (
        settings.resolved_oauth_start_url
        == "https://linebot-lab.example.dev/oauth/start?secret=the-secret"
    )


def test_start_url_is_never_relative(make_settings):
    """A relative link resolves against the admin origin, which has no
    /oauth/start route -- that was the 404."""
    settings = make_settings(public_base_url="https://x.example", ms_redirect_uri=None)
    assert settings.resolved_oauth_start_url.startswith("https://")


def test_start_and_callback_always_share_an_origin(make_settings):
    settings = make_settings(
        public_base_url=None,
        ms_redirect_uri="https://override.example/oauth/callback",
        oauth_setup_secret="s",
    )
    start, callback = settings.resolved_oauth_start_url, settings.resolved_redirect_uri
    assert start == "https://override.example/oauth/start?secret=s"
    assert start.rsplit("/", 1)[0] == callback.rsplit("/", 1)[0]


def test_start_url_none_when_origin_cannot_be_inferred(make_settings):
    """An override that isn't a /oauth/callback URL: better no link than a
    broken one -- the page explains instead."""
    settings = make_settings(
        public_base_url=None,
        ms_redirect_uri="https://odd.example/somewhere-else",
        oauth_setup_secret="s",
    )
    assert settings.resolved_oauth_start_url is None


def test_start_url_none_without_a_secret(make_settings):
    settings = make_settings(
        public_base_url="https://x.example", ms_redirect_uri=None, oauth_setup_secret=None
    )
    assert settings.resolved_oauth_start_url is None


def test_admin_app_really_has_no_oauth_routes(make_app_state):
    """Pins the asymmetry the bug came from, so the fix isn't 'corrected'
    later by making the link relative again."""
    state = make_app_state()
    admin_paths = {r.path for r in build_admin_app(state).routes}
    public_paths = {r.path for r in build_public_app(state).routes}
    assert "/oauth/start" not in admin_paths
    assert "/oauth/callback" not in admin_paths
    assert "/oauth/start" in public_paths
    assert "/oauth/callback" in public_paths


# ------------------------------------------------ webhook diagnosis (bug 2) --


def test_fingerprint_is_stable_and_not_the_secret():
    assert secret_fingerprint("abc") == secret_fingerprint("abc")
    assert secret_fingerprint("abc") != secret_fingerprint("abd")
    assert "abc" not in secret_fingerprint("abc")
    assert len(secret_fingerprint("abc")) == 8


def test_fingerprint_handles_unset_secret():
    assert secret_fingerprint(None) == "<unset>"
    assert secret_fingerprint("") == "<unset>"


def test_mismatched_secret_logs_the_fingerprint_and_the_likely_cause(
    public_client_factory, caplog
):
    client, _ = public_client_factory(line_channel_secret="the-apps-stale-secret")
    with caplog.at_level(logging.WARNING, logger="app.main"):
        r = client.post(
            "/line/webhook",
            content=VERIFY_BODY,
            headers={
                "x-line-signature": _sign("the-consoles-current-secret", VERIFY_BODY),
                "content-type": "application/json",
            },
        )
    assert r.status_code == 400
    msg = " ".join(rec.getMessage() for rec in caplog.records)
    assert secret_fingerprint("the-apps-stale-secret") in msg
    assert "the-apps-stale-secret" not in msg  # never log the secret itself
    assert "Setup > LINE" in msg


def test_missing_header_is_reported_differently_from_a_mismatch(
    public_client_factory, caplog
):
    client, _ = public_client_factory(line_channel_secret="s")
    with caplog.at_level(logging.WARNING, logger="app.main"):
        r = client.post(
            "/line/webhook", content=VERIFY_BODY, headers={"content-type": "application/json"}
        )
    assert r.status_code == 400
    msg = " ".join(rec.getMessage() for rec in caplog.records)
    assert "no X-Line-Signature header" in msg
    assert "did not match" not in msg


def test_successful_verify_is_logged(public_client_factory, caplog):
    secret = "matching-secret"
    client, _ = public_client_factory(line_channel_secret=secret)
    with caplog.at_level(logging.INFO, logger="app.main"):
        r = client.post(
            "/line/webhook",
            content=VERIFY_BODY,
            headers={
                "x-line-signature": _sign(secret, VERIFY_BODY),
                "content-type": "application/json",
            },
        )
    assert r.status_code == 200
    msg = " ".join(rec.getMessage() for rec in caplog.records)
    assert "Verify" in msg and "200" in msg


def test_real_events_still_processed_after_the_empty_events_branch(public_client_factory):
    """The empty-events log must not short-circuit real traffic."""
    secret = "matching-secret"
    group = "Ctestgroup0000000000000000000000"
    payload = json.dumps(
        {
            "destination": "U0",
            "events": [
                {
                    "type": "message",
                    "source": {"groupId": group},
                    "message": {"type": "image", "id": "msg-1"},
                    "timestamp": 1757000000000,
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    client, state = public_client_factory(line_channel_secret=secret, line_lab_group_id=group)
    r = client.post(
        "/line/webhook",
        content=payload,
        headers={"x-line-signature": _sign(secret, payload), "content-type": "application/json"},
    )
    assert r.status_code == 200
    assert state.queue.qsize() == 1


def test_setup_page_renders_the_absolute_link(admin_client_factory):
    """The rendered HTML must carry the absolute URL. A relative href here is
    exactly the 404 -- it resolves against the admin origin, which has no
    /oauth/start route."""
    from app.auth import set_admin_password

    client, state = admin_client_factory(
        public_base_url="https://linebot-lab.example.dev",
        ms_redirect_uri=None,
        oauth_setup_secret="the-secret",
    )
    set_admin_password(state.config_store, "test-password-123")
    resp = client.post("/login", data={"password": "test-password-123"}, follow_redirects=False)
    assert resp.status_code == 303, "fixture failed to authenticate"

    body = client.get("/setup/onedrive").text
    assert "Sign in to OneDrive" in body, "not the setup page -- login likely failed"
    assert "https://linebot-lab.example.dev/oauth/start?secret=the-secret" in body
    assert 'href="/oauth/start' not in body  # the relative form must be gone
