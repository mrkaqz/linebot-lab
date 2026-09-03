"""Shared pytest fixtures. Tests run with no network and no API keys set --
Settings() is always constructed with explicit kwargs here, never from a
real .env file or the process environment, so tests are hermetic.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `import app...` work when pytest is run from the linebot-lab/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.config import Settings


@pytest.fixture
def make_settings(tmp_path):
    """Factory fixture: make_settings(**overrides) -> Settings, with sane
    hermetic defaults for every required field.
    """

    def _make(**overrides) -> Settings:
        defaults = dict(
            line_channel_secret="test-channel-secret",
            line_channel_access_token="test-access-token",
            line_lab_group_id="Ctestgroup0000000000000000000000",
            ocr_backend="tesseract",
            opd_regex=Settings.model_fields["opd_regex"].default,
            timezone="Asia/Bangkok",
            onedrive_root="/LabResults",
            ms_client_id="test-ms-client-id",
            ms_redirect_uri="https://example.com/oauth/callback",
            oauth_setup_secret="test-setup-secret",
            data_dir=tmp_path,
            log_level="INFO",
            _env_file=None,  # never read a real .env during tests
        )
        defaults.update(overrides)
        return Settings(**defaults)

    return _make


@pytest.fixture
def make_app_state(tmp_path, make_settings):
    """Factory fixture: make_app_state(**settings_overrides) -> AppState,
    fully built (ConfigStore, Store, LINE/OneDrive clients, MarkItDown)
    against an isolated tmp_path data dir -- no network, no real .env.
    """
    from app.runtime import AppState

    def _make(**settings_overrides) -> "AppState":
        settings = make_settings(**settings_overrides)
        return AppState.create(settings)

    return _make


@pytest.fixture
def admin_client_factory(make_app_state):
    """Factory fixture: admin_client_factory(**settings_overrides) ->
    (TestClient, AppState) for the admin app alone (port-8001 routes only).
    """
    from fastapi.testclient import TestClient

    from app.main import build_admin_app

    def _make(**settings_overrides):
        state = make_app_state(**settings_overrides)
        app = build_admin_app(state)
        client = TestClient(app)
        return client, state

    return _make


@pytest.fixture
def public_client_factory(make_app_state):
    """Factory fixture: public_client_factory(**settings_overrides) ->
    (TestClient, AppState) for the public app alone (port-8000 routes only).
    """
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import build_public_app

    def _make(**settings_overrides):
        state = make_app_state(**settings_overrides)
        state.queue = asyncio.Queue()
        app = build_public_app(state)
        client = TestClient(app)
        return client, state

    return _make
