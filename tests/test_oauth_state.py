"""The OAuth anti-hijack secret travels in the `state` parameter.

It used to ride on the redirect URI as `?secret=...`. That cannot work:
Entra rejects any redirect URI containing a query string for app
registrations that sign in personal Microsoft accounts, whatever the
platform type -- and personal OneDrive supports only delegated auth, so
that is the registration this app must use.

`state` is the documented way to carry your own value across the redirect
and is not subject to that restriction. Microsoft returns it untouched, and
MSAL additionally pins it to the pending flow and rejects a callback whose
state does not match, so CSRF protection is preserved.

The two endpoints are guarded differently on purpose:

  /oauth/start     ?secret=...   -- never sent to Microsoft, so a query
                                    string is unrestricted here
  /oauth/callback  ?state=...    -- Microsoft controls this URL's query
                                    string, so the secret must ride in state
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.main import _oauth_state_value, _require_callback_state, _require_setup_secret

SECRET = "test-setup-secret"  # matches the conftest make_settings default


class _Req:
    """Minimal stand-in for the bits of Request the guards touch."""

    def __init__(self, **params):
        self.query_params = params


# ------------------------------------------------------------ state value --


def test_state_value_contains_the_secret_and_a_nonce():
    state = _oauth_state_value(SECRET)
    assert state.startswith(SECRET + ".")
    nonce = state[len(SECRET) + 1:]
    assert nonce and "." not in nonce


def test_state_value_differs_every_call():
    """The setup secret is long-lived, so without a per-attempt nonce every
    authorization would present an identical, replayable state."""
    assert len({_oauth_state_value(SECRET) for _ in range(50)}) == 50


# ------------------------------------------------------- callback guard ----


def test_callback_accepts_state_produced_by_start(make_settings):
    settings = make_settings(oauth_setup_secret=SECRET)
    _require_callback_state(_Req(state=_oauth_state_value(SECRET)), settings)  # must not raise


def test_callback_rejects_wrong_secret_in_state(make_settings):
    settings = make_settings(oauth_setup_secret=SECRET)
    with pytest.raises(HTTPException) as exc:
        _require_callback_state(_Req(state=_oauth_state_value("not-the-secret")), settings)
    assert exc.value.status_code == 403


def test_callback_rejects_missing_state(make_settings):
    settings = make_settings(oauth_setup_secret=SECRET)
    with pytest.raises(HTTPException) as exc:
        _require_callback_state(_Req(), settings)
    assert exc.value.status_code == 403


def test_callback_rejects_state_without_a_nonce_separator(make_settings):
    """A bare secret with no '.' must not pass -- rpartition yields an empty
    secret in that case, which must fail rather than match vacuously."""
    settings = make_settings(oauth_setup_secret=SECRET)
    with pytest.raises(HTTPException) as exc:
        _require_callback_state(_Req(state=SECRET), settings)
    assert exc.value.status_code == 403


def test_callback_ignores_the_old_secret_query_param(make_settings):
    """Regression guard: the pre-fix callback read ?secret=. If that path
    ever came back it would pass a request Microsoft can never send."""
    settings = make_settings(oauth_setup_secret=SECRET)
    with pytest.raises(HTTPException) as exc:
        _require_callback_state(_Req(secret=SECRET), settings)
    assert exc.value.status_code == 403


def test_callback_rejects_when_no_secret_configured(make_settings):
    settings = make_settings(oauth_setup_secret=None)
    with pytest.raises(HTTPException) as exc:
        _require_callback_state(_Req(state="anything.nonce"), settings)
    assert exc.value.status_code == 403


def test_secret_containing_dots_still_round_trips(make_settings):
    """rpartition splits on the LAST dot, and the nonce never contains one,
    so an operator-supplied secret with dots in it still validates."""
    dotted = "some.secret.with.dots"
    settings = make_settings(oauth_setup_secret=dotted)
    _require_callback_state(_Req(state=_oauth_state_value(dotted)), settings)


# ---------------------------------------------------------- start guard ----


def test_start_still_uses_the_secret_query_param(make_settings):
    """/oauth/start is opened from the admin UI and never registered with
    Microsoft, so it keeps the plain ?secret= gate."""
    settings = make_settings(oauth_setup_secret=SECRET)
    _require_setup_secret(_Req(secret=SECRET), settings)  # must not raise


def test_start_rejects_wrong_secret(make_settings):
    settings = make_settings(oauth_setup_secret=SECRET)
    with pytest.raises(HTTPException) as exc:
        _require_setup_secret(_Req(secret="nope"), settings)
    assert exc.value.status_code == 403


def test_start_rejects_missing_secret(make_settings):
    settings = make_settings(oauth_setup_secret=SECRET)
    with pytest.raises(HTTPException) as exc:
        _require_setup_secret(_Req(), settings)
    assert exc.value.status_code == 403


# ------------------------------------------------------- end-to-end route --


def test_start_route_redirects_and_passes_state_to_msal(public_client_factory, monkeypatch):
    client, state_obj = public_client_factory(oauth_setup_secret=SECRET)
    seen = {}

    def fake_start_auth(state=None):
        seen["state"] = state
        return "https://login.microsoftonline.com/authorize?fake=1"

    monkeypatch.setattr(state_obj.onedrive, "start_auth", fake_start_auth)

    r = client.get(f"/oauth/start?secret={SECRET}", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert seen["state"].startswith(SECRET + ".")


def test_callback_route_rejects_bad_state(public_client_factory):
    client, _ = public_client_factory(oauth_setup_secret=SECRET)
    r = client.get("/oauth/callback?code=abc&state=wrong.nonce")
    assert r.status_code == 403


def test_callback_route_accepts_good_state(public_client_factory, monkeypatch):
    client, state_obj = public_client_factory(oauth_setup_secret=SECRET)
    monkeypatch.setattr(state_obj.onedrive, "complete_auth", lambda params: None)
    r = client.get(f"/oauth/callback?code=abc&state={_oauth_state_value(SECRET)}")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
