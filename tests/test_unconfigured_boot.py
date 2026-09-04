"""Zero-required-env-vars boot: Settings() constructs with nothing set,
both apps build and serve /healthz/login, the webhook fails safe with no
channel secret, oauth_setup_secret is generated once and stays stable,
derived OAuth/webhook URLs are correct, and missing_requirements() reports
the right groups for partial configs. See app/config.py, app/runtime.py,
app/settings_store.py, app/main.py.
"""

from __future__ import annotations

import json

import pytest

from app.config import GROUP_LINE, GROUP_OCR, GROUP_ONEDRIVE, Settings
from app.runtime import AppState


# ---------------------------------------------------------------- Settings --


def test_settings_construct_with_empty_environment(monkeypatch, tmp_path):
    """No env vars at all (not even the five that used to be
    pydantic-required) -- Settings() must not raise."""
    for key in (
        "LINE_CHANNEL_SECRET",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_LAB_GROUP_ID",
        "MS_CLIENT_ID",
        "MS_REDIRECT_URI",
        "PUBLIC_BASE_URL",
        "OAUTH_SETUP_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings(data_dir=tmp_path, _env_file=None)

    assert settings.line_channel_secret is None
    assert settings.line_channel_access_token is None
    assert settings.line_lab_group_id is None
    assert settings.ms_client_id is None
    assert settings.ms_redirect_uri is None
    assert settings.public_base_url is None
    assert settings.oauth_setup_secret is None
    assert settings.ocr_backend == "tesseract"  # default needs no credentials


def test_missing_requirements_empty_for_fully_configured(make_settings):
    settings = make_settings()  # make_settings fixture fills every field
    assert settings.missing_requirements(onedrive_authorized=True) == {}


def test_missing_requirements_reports_line_group(make_settings):
    settings = make_settings(
        line_channel_secret="s", line_channel_access_token="t", line_lab_group_id=None
    )
    missing = settings.missing_requirements()
    assert missing[GROUP_LINE] == ["LINE lab group id"]


def test_missing_requirements_reports_line_credentials(make_settings):
    settings = make_settings(line_channel_secret=None, line_channel_access_token=None, line_lab_group_id=None)
    missing = settings.missing_requirements()
    assert "LINE channel secret" in missing[GROUP_LINE]
    assert "LINE channel access token" in missing[GROUP_LINE]
    assert "LINE lab group id" in missing[GROUP_LINE]


def test_missing_requirements_reports_onedrive_client_id_and_base_url(make_settings):
    settings = make_settings(ms_client_id=None, ms_redirect_uri=None, public_base_url=None)
    missing = settings.missing_requirements()
    assert "Microsoft Entra application (client) ID" in missing[GROUP_ONEDRIVE]
    assert "Public base URL" in missing[GROUP_ONEDRIVE]


def test_missing_requirements_public_base_url_not_required_when_explicit_redirect_set(make_settings):
    settings = make_settings(ms_redirect_uri="https://example.com/oauth/callback", public_base_url=None)
    missing = settings.missing_requirements()
    assert GROUP_ONEDRIVE not in missing  # explicit override covers it


def test_missing_requirements_reports_onedrive_sign_in_when_not_authorized(make_settings):
    settings = make_settings(ms_client_id="c", public_base_url="https://example.com", ms_redirect_uri=None)
    missing = settings.missing_requirements(onedrive_authorized=False)
    assert missing[GROUP_ONEDRIVE] == ["OneDrive sign-in"]


def test_missing_requirements_onedrive_sign_in_omitted_when_unknown(make_settings):
    """Passing onedrive_authorized=None (the default) -- no live OneDriveClient
    at hand, e.g. a plain Settings-only test -- must not claim a sign-in is
    missing (it simply doesn't know)."""
    settings = make_settings(ms_client_id="c", public_base_url="https://example.com", ms_redirect_uri=None)
    missing = settings.missing_requirements()
    assert GROUP_ONEDRIVE not in missing


def test_missing_requirements_reports_ocr_key_for_claude(make_settings):
    settings = make_settings(ocr_backend="claude", anthropic_api_key=None)
    missing = settings.missing_requirements()
    assert missing[GROUP_OCR] == ["Anthropic API key"]


def test_missing_requirements_reports_ocr_key_for_gemini(make_settings):
    settings = make_settings(ocr_backend="gemini", gemini_api_key=None)
    missing = settings.missing_requirements()
    assert missing[GROUP_OCR] == ["Gemini API key"]


def test_missing_requirements_tesseract_needs_nothing(make_settings):
    settings = make_settings(ocr_backend="tesseract", anthropic_api_key=None, gemini_api_key=None)
    missing = settings.missing_requirements()
    assert GROUP_OCR not in missing


# ---------------------------------------------------------- derived URLs ----


@pytest.mark.parametrize("base_url", ["https://abc123.trycloudflare.com", "https://abc123.trycloudflare.com/"])
def test_derived_redirect_uri_and_webhook_url(make_settings, base_url):
    settings = make_settings(
        public_base_url=base_url, ms_redirect_uri=None, oauth_setup_secret="the-secret"
    )
    assert settings.resolved_redirect_uri == "https://abc123.trycloudflare.com/oauth/callback"
    assert settings.line_webhook_url == "https://abc123.trycloudflare.com/line/webhook"


def test_derived_redirect_uri_carries_no_query_string(make_settings):
    """Entra rejects a redirect URI containing a query string for app
    registrations that sign in personal Microsoft accounts -- which is the
    only kind personal OneDrive supports. The setup secret therefore travels
    in the OAuth state parameter, never in the redirect URI."""
    settings = make_settings(
        public_base_url="https://abc123.trycloudflare.com",
        ms_redirect_uri=None,
        oauth_setup_secret="the-secret",
    )
    assert "?" not in settings.resolved_redirect_uri
    assert "the-secret" not in settings.resolved_redirect_uri


def test_derived_redirect_uri_no_longer_depends_on_the_secret(make_settings):
    """It used to require oauth_setup_secret because it embedded it."""
    settings = make_settings(
        public_base_url="https://abc123.trycloudflare.com",
        ms_redirect_uri=None,
        oauth_setup_secret=None,
    )
    assert settings.resolved_redirect_uri == "https://abc123.trycloudflare.com/oauth/callback"


def test_explicit_ms_redirect_uri_overrides_derived(make_settings):
    settings = make_settings(
        public_base_url="https://abc123.trycloudflare.com",
        ms_redirect_uri="https://explicit.example.com/oauth/callback",
        oauth_setup_secret="the-secret",
    )
    assert settings.resolved_redirect_uri == "https://explicit.example.com/oauth/callback"


def test_resolved_redirect_uri_none_when_unconfigured(make_settings):
    settings = make_settings(public_base_url=None, ms_redirect_uri=None, oauth_setup_secret=None)
    assert settings.resolved_redirect_uri is None
    assert settings.line_webhook_url is None


def test_public_base_url_rejects_non_http_scheme(make_settings):
    with pytest.raises(Exception):
        make_settings(public_base_url="ftp://example.com")


def test_public_base_url_rejects_missing_host(make_settings):
    with pytest.raises(Exception):
        make_settings(public_base_url="https://")


# ---------------------------------------------------------- oauth secret ----


def test_oauth_setup_secret_generated_once_and_stable_across_restarts(tmp_path, make_settings):
    settings1 = make_settings(oauth_setup_secret=None, data_dir=tmp_path)
    state1 = AppState.create(settings1)
    secret1 = state1.settings.oauth_setup_secret
    assert secret1  # something was generated
    assert len(secret1) > 20

    # Simulate a restart: rebuild AppState fresh against the SAME data dir.
    settings2 = make_settings(oauth_setup_secret=None, data_dir=tmp_path)
    state2 = AppState.create(settings2)
    assert state2.settings.oauth_setup_secret == secret1


def test_env_provided_oauth_setup_secret_wins_over_generation(tmp_path, make_settings):
    settings = make_settings(oauth_setup_secret="from-the-environment", data_dir=tmp_path)
    state = AppState.create(settings)
    assert state.settings.oauth_setup_secret == "from-the-environment"


# ------------------------------------------------------------- apps/healthz-


def test_both_apps_build_when_nothing_configured(make_app_state):
    """AppState.create() and both build_public_app/build_admin_app must not
    raise when every previously-required field is unset."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import build_admin_app, build_public_app

    state = make_app_state(
        line_channel_secret=None,
        line_channel_access_token=None,
        line_lab_group_id=None,
        ms_client_id=None,
        ms_redirect_uri=None,
        public_base_url=None,
        oauth_setup_secret=None,
    )
    state.queue = asyncio.Queue()

    public_client = TestClient(build_public_app(state))
    admin_client = TestClient(build_admin_app(state))

    assert public_client.get("/healthz").status_code == 200
    assert admin_client.get("/login").status_code == 200


def test_healthz_reports_unconfigured(public_client_factory):
    client, state = public_client_factory(
        line_channel_secret=None,
        line_channel_access_token=None,
        line_lab_group_id=None,
        ms_client_id=None,
        ms_redirect_uri=None,
        public_base_url=None,
        oauth_setup_secret=None,
    )
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert GROUP_LINE in body["missing"]
    assert GROUP_ONEDRIVE in body["missing"]


def test_healthz_configured_true_when_everything_set(public_client_factory):
    client, state = public_client_factory()  # make_settings fixture defaults are all filled in
    resp = client.get("/healthz")
    body = resp.json()
    # onedrive isn't actually signed in in a hermetic test -- that alone
    # keeps this "unconfigured", but the LINE/OCR/client-id/base-url groups
    # must all be clean.
    assert GROUP_LINE not in body["missing"]
    assert GROUP_OCR not in body["missing"]


# ------------------------------------------------------------- /line/webhook


def test_webhook_returns_200_and_processes_nothing_when_secret_unset(public_client_factory):
    client, state = public_client_factory(line_channel_secret=None, line_channel_access_token=None)

    resp = client.post(
        "/line/webhook",
        content=json.dumps({"events": [{"type": "message", "message": {"type": "image", "id": "m1"}, "source": {"groupId": state.settings.line_lab_group_id}, "timestamp": 1756900000000}]}).encode("utf-8"),
        headers={"x-line-signature": "irrelevant", "content-type": "application/json"},
    )

    assert resp.status_code == 200
    assert state.queue.qsize() == 0


def test_webhook_does_not_accept_unverified_events_once_secret_is_set(public_client_factory):
    """Guard against a regression where the "no secret configured" fast
    path accidentally also short-circuits signature verification once a
    secret IS configured."""
    client, state = public_client_factory()  # channel secret is set by make_settings defaults

    resp = client.post(
        "/line/webhook",
        content=json.dumps({"events": []}).encode("utf-8"),
        headers={"x-line-signature": "definitely-not-valid", "content-type": "application/json"},
    )

    assert resp.status_code == 400


# ------------------------------------------------------- oauth setup guard --


def test_require_setup_secret_rejects_when_unconfigured(make_settings):
    """Unit-level check of the defensive branch in app.main._require_setup_secret:
    a Settings with no oauth_setup_secret (bypassing AppState.create(), which
    would otherwise auto-generate one -- see app.settings_store.ensure_oauth_setup_secret)
    must be rejected with 403, never crash inside hmac.compare_digest on None.
    """
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.main import _require_setup_secret

    settings = make_settings(oauth_setup_secret=None)
    scope = {"type": "http", "query_string": b"secret=whatever", "headers": []}
    request = Request(scope)

    with pytest.raises(HTTPException) as exc_info:
        _require_setup_secret(request, settings)
    assert exc_info.value.status_code == 403


def test_oauth_setup_secret_is_auto_generated_by_the_time_the_app_serves_requests(public_client_factory):
    """In real use (through AppState.create(), which every app-building
    fixture goes through) oauth_setup_secret is never actually left unset --
    it's auto-generated on first boot -- so /oauth/start requires the
    (generated) secret, not open access."""
    client, state = public_client_factory(oauth_setup_secret=None)
    assert state.settings.oauth_setup_secret  # auto-generated by AppState.create()

    resp = client.get("/oauth/start", follow_redirects=False)
    assert resp.status_code == 403  # wrong/missing secret in the query string

    resp = client.get(f"/oauth/start?secret={state.settings.oauth_setup_secret}", follow_redirects=False)
    assert resp.status_code in (302, 307)  # secret accepted -- redirected into the MSAL auth flow


# --------------------------------------------------------- OCR worker safety


@pytest.mark.asyncio
async def test_ocr_job_fails_cleanly_when_claude_backend_unconfigured(make_app_state, tmp_path):
    """OCR_BACKEND=claude with no ANTHROPIC_API_KEY must not crash
    AppState.create() (build_markitdown falls back to a placeholder
    converter -- see app.extract._NotConfiguredConverter) and must not crash
    a job that reaches it -- process_image_event already treats an
    extraction exception as "file to _UNFILED", not a worker crash.
    """
    from app.line_client import LineClient
    from app.pipeline import process_image_event

    state = make_app_state(ocr_backend="claude", anthropic_api_key=None)  # must not raise

    class _FakeLineClient(LineClient):
        def __init__(self):
            pass

        async def download_content(self, message_id, dest_path):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(b"not-real-jpeg-bytes-but-right-extension")

    class _FakeOneDrive:
        async def resolve_item_path(self, item_id):
            raise AssertionError("not used in this test")

        async def upload_pair(self, folder_path, base_stem, jpg_bytes, md_text, jpg_ext="jpg"):
            return f"{folder_path}/{base_stem}.jpg", f"{folder_path}/{base_stem}.md"

    event = {
        "message": {"id": "m1", "type": "image"},
        "timestamp": 1756900000000,
        "source": {"groupId": "Ctestgroup0000000000000000000000"},
    }

    # Must complete without raising -- a raise here is exactly what would
    # take down app.main._worker's queue-draining loop for good.
    await process_image_event(
        event,
        settings=state.settings,
        line_client=_FakeLineClient(),
        markitdown=state.markitdown,
        onedrive=_FakeOneDrive(),
        store=state.store,
    )

    unfiled_rows = list(state.store.list_unfiled(resolved=False))
    assert len(unfiled_rows) == 1
    assert unfiled_rows[0]["message_id"] == "m1"


# ------------------------------------------------- hot reload: OneDrive/MSAL


def test_changing_ms_client_id_via_ui_rebuilds_msal_client(admin_client_factory):
    from app.auth import set_admin_password

    client, state = admin_client_factory(public_base_url="https://before.example.com", ms_redirect_uri=None)
    set_admin_password(state.config_store, "test-password-123")
    client.post("/login", data={"password": "test-password-123"}, follow_redirects=False)

    original_onedrive = state.onedrive

    response = client.post(
        "/setup/onedrive/save",
        data={
            "ms_client_id": "brand-new-client-id",
            "public_base_url": "https://after.example.com",
            "ms_redirect_uri": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert state.settings.ms_client_id == "brand-new-client-id"
    assert state.settings.public_base_url == "https://after.example.com"
    # A NEW OneDriveClient instance was built in place -- the MSAL app
    # inside it now targets the new client id, with no restart.
    assert state.onedrive is not original_onedrive


def test_onedrive_save_rejects_invalid_public_base_url(admin_client_factory):
    from app.auth import set_admin_password

    client, state = admin_client_factory()
    set_admin_password(state.config_store, "test-password-123")
    client.post("/login", data={"password": "test-password-123"}, follow_redirects=False)

    response = client.post(
        "/setup/onedrive/save",
        data={"ms_client_id": "c", "public_base_url": "not-a-url", "ms_redirect_uri": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert state.config_store.is_set("public_base_url") is False  # rejected, nothing written


def test_regenerate_oauth_setup_secret_changes_it_and_rebuilds_onedrive(admin_client_factory):
    from app.auth import set_admin_password

    client, state = admin_client_factory()
    set_admin_password(state.config_store, "test-password-123")
    client.post("/login", data={"password": "test-password-123"}, follow_redirects=False)

    original_secret = state.settings.oauth_setup_secret
    original_onedrive = state.onedrive

    response = client.post("/setup/onedrive/regenerate-secret", follow_redirects=False)

    assert response.status_code == 303
    assert state.settings.oauth_setup_secret != original_secret
    assert state.onedrive is not original_onedrive
