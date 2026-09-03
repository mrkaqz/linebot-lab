"""End-to-end (through the HTTP routes, not just ConfigStore directly)
coverage for the setup pages: blank secret fields leave stored values
unchanged, Clear actually clears, and invalid General input is rejected.
"""

from __future__ import annotations

from app.auth import set_admin_password


def _login(client, state, password="test-password-123"):
    set_admin_password(state.config_store, password)
    client.post("/login", data={"password": password}, follow_redirects=False)


def test_ocr_save_blank_key_leaves_existing_key_unchanged(admin_client_factory):
    client, state = admin_client_factory()
    _login(client, state)

    client.post(
        "/setup/ocr/save",
        data={"backend": "claude", "claude_model": "", "anthropic_api_key": "sk-original", "gemini_model": "", "gemini_api_key": ""},
    )
    assert state.config_store.get("anthropic_api_key") == "sk-original"

    # Re-submit with the key field left blank (as the masked UI always
    # does on a save that doesn't touch it) -- must NOT clear it.
    client.post(
        "/setup/ocr/save",
        data={"backend": "claude", "claude_model": "", "anthropic_api_key": "", "gemini_model": "", "gemini_api_key": ""},
    )
    assert state.config_store.get("anthropic_api_key") == "sk-original"


def test_ocr_save_clear_checkbox_actually_clears(admin_client_factory):
    client, state = admin_client_factory()
    _login(client, state)

    client.post(
        "/setup/ocr/save",
        data={"backend": "claude", "claude_model": "", "anthropic_api_key": "sk-original", "gemini_model": "", "gemini_api_key": ""},
    )
    assert state.config_store.is_set("anthropic_api_key") is True

    client.post(
        "/setup/ocr/save",
        data={
            "backend": "claude",
            "claude_model": "",
            "anthropic_api_key": "",
            "clear_anthropic_api_key": "1",
            "gemini_model": "",
            "gemini_api_key": "",
        },
    )
    assert state.config_store.is_set("anthropic_api_key") is False


def test_line_save_blank_secret_leaves_existing_secret_unchanged(admin_client_factory):
    client, state = admin_client_factory()
    _login(client, state)

    client.post(
        "/setup/line/save",
        data={"channel_secret": "original-secret", "channel_access_token": "original-token", "group_id": "Cxxx", "admin_line_id": ""},
    )
    assert state.config_store.get("line_channel_secret") == "original-secret"

    client.post(
        "/setup/line/save",
        data={"channel_secret": "", "channel_access_token": "", "group_id": "Cxxx", "admin_line_id": ""},
    )
    assert state.config_store.get("line_channel_secret") == "original-secret"
    assert state.config_store.get("line_channel_access_token") == "original-token"


def test_general_save_rejects_invalid_timezone(admin_client_factory):
    client, state = admin_client_factory()
    _login(client, state)

    response = client.post(
        "/setup/general/save",
        data={"timezone": "Not/AZone", "opd_regex": ".*", "setup_ui_exposure": "lan"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert state.config_store.is_set("timezone") is False  # rejected, nothing written


def test_general_save_rejects_invalid_regex(admin_client_factory):
    client, state = admin_client_factory()
    _login(client, state)

    response = client.post(
        "/setup/general/save",
        data={"timezone": "UTC", "opd_regex": "(unclosed", "setup_ui_exposure": "lan"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert state.config_store.is_set("opd_regex") is False


def test_general_save_accepts_valid_input_and_hot_reloads(admin_client_factory):
    client, state = admin_client_factory()
    _login(client, state)

    response = client.post(
        "/setup/general/save",
        data={"timezone": "UTC", "opd_regex": "OPD (\\d+)", "setup_ui_exposure": "lan"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert state.settings.timezone == "UTC"
    assert state.settings.opd_regex == "OPD (\\d+)"


def test_password_change_requires_correct_current_password(admin_client_factory):
    client, state = admin_client_factory()
    _login(client, state, password="current-pw-12345")

    response = client.post(
        "/setup/general/password",
        data={"current_password": "wrong", "new_password": "newpassword1", "confirm_password": "newpassword1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    from app.auth import check_admin_password

    assert check_admin_password(state.config_store, "current-pw-12345") is True
    assert check_admin_password(state.config_store, "newpassword1") is False
