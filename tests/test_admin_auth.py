"""Every admin route (except /login and static assets) must reject an
unauthenticated request, and the admin router must be mounted on the public
app only when SETUP_UI_EXPOSURE=public.
"""

from __future__ import annotations

import pytest

from app.auth import set_admin_password

# (method, path, form_body) -- form_body is a minimal, validly-shaped body so
# an unauthenticated request is rejected for AUTH, not a 422 on missing
# required form fields.
PAGE_ROUTES = [
    ("GET", "/", None),
    ("GET", "/setup", None),
    ("GET", "/setup/line", None),
    ("GET", "/setup/ocr", None),
    ("GET", "/setup/onedrive", None),
    ("GET", "/setup/general", None),
    ("GET", "/unfiled", None),
    ("POST", "/setup/line/save", {"channel_secret": "", "channel_access_token": "", "group_id": "", "admin_line_id": ""}),
    ("POST", "/setup/ocr/save", {"backend": "tesseract", "claude_model": "", "anthropic_api_key": "", "gemini_model": "", "gemini_api_key": ""}),
    ("POST", "/setup/onedrive/select", {"item_id": "x", "path": "/x"}),
    ("POST", "/setup/onedrive/disconnect", {}),
    ("POST", "/setup/general/save", {"timezone": "UTC", "opd_regex": ".*", "setup_ui_exposure": "lan"}),
    ("POST", "/setup/general/password", {"current_password": "a", "new_password": "bbbbbbbb", "confirm_password": "bbbbbbbb"}),
    ("POST", "/unfiled/1/resolve", {"opd_number": "12345"}),
    ("POST", "/unfiled/1/dismiss", {}),
]

JSON_ROUTES = [
    ("GET", "/setup/line/detect/status", None),
    ("POST", "/setup/line/detect/start", {}),
    ("POST", "/setup/ocr/test", {"backend": "tesseract", "api_key": ""}),
    ("GET", "/setup/onedrive/browse", None),
    ("POST", "/setup/onedrive/new-folder", {"parent_item_id": "", "name": "x"}),
    ("POST", "/setup/general/test-regex", {"pattern": ".*", "sample_text": "x"}),
    ("GET", "/unfiled/1/photo", None),
]


def _call(client, method, path, body):
    if method == "GET":
        return client.get(path, follow_redirects=False)
    return client.post(path, data=body, follow_redirects=False)


@pytest.mark.parametrize("method,path,body", PAGE_ROUTES)
def test_page_route_rejects_unauthenticated_request(admin_client_factory, method, path, body):
    client, _state = admin_client_factory()
    response = _call(client, method, path, body)
    assert response.status_code in (303, 401, 403)
    if response.status_code == 303:
        assert response.headers["location"] == "/login"


@pytest.mark.parametrize("method,path,body", JSON_ROUTES)
def test_json_route_rejects_unauthenticated_request(admin_client_factory, method, path, body):
    client, _state = admin_client_factory()
    response = _call(client, method, path, body)
    assert response.status_code == 401


def test_login_page_itself_is_reachable_without_auth(admin_client_factory):
    client, _state = admin_client_factory()
    response = client.get("/login")
    assert response.status_code == 200


def test_after_login_dashboard_is_reachable(admin_client_factory):
    client, state = admin_client_factory()
    set_admin_password(state.config_store, "test-password-123")
    login = client.post("/login", data={"password": "test-password-123"}, follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "/"

    dashboard = client.get("/")
    assert dashboard.status_code == 200


def test_wrong_password_does_not_authenticate(admin_client_factory):
    client, state = admin_client_factory()
    set_admin_password(state.config_store, "correct-password")
    client.post("/login", data={"password": "wrong-password"}, follow_redirects=False)

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_lockout_after_repeated_failures(admin_client_factory):
    client, state = admin_client_factory()
    set_admin_password(state.config_store, "correct-password")

    for _ in range(5):
        client.post("/login", data={"password": "wrong"}, follow_redirects=False)

    # Even the correct password is now rejected until the lockout expires.
    response = client.post("/login", data={"password": "correct-password"}, follow_redirects=False)
    assert response.status_code == 303
    dashboard = client.get("/", follow_redirects=False)
    assert dashboard.status_code == 303  # still not logged in


# ------------------------------------------------ lan vs public exposure --


def test_lan_mode_does_not_mount_admin_routes_on_public_app(public_client_factory):
    client, _state = public_client_factory(setup_ui_exposure="lan")
    response = client.get("/login")
    assert response.status_code == 404
    response = client.get("/")
    assert response.status_code == 404


def test_public_mode_mounts_admin_routes_on_public_app(public_client_factory):
    client, _state = public_client_factory(setup_ui_exposure="public")
    response = client.get("/login")
    assert response.status_code == 200


def test_public_app_always_exposes_webhook_and_health_regardless_of_exposure(public_client_factory):
    client, _state = public_client_factory(setup_ui_exposure="lan")
    assert client.get("/healthz").status_code == 200
