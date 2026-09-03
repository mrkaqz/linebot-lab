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
