"""Shared, mutable application state: the one place both the public app
(port 8000) and the admin app (port 8001) reach into for the store, the
OneDrive/LINE clients, MarkItDown, the processing queue, and the current
Settings -- built ONCE at process startup (see app/main.py) and attached to
both FastAPI apps, rather than each app doing its own competing lifespan.

Config changes made through the admin UI go through `AppState.save_setting`
/ `apply_changes`, which write to the DB-backed ConfigStore, reload
Settings (DB > env > default), and rebuild whichever runtime objects the
changed fields actually affect ("hot reload") -- see each method's
docstring for exactly what is and isn't hot-applicable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from markitdown import MarkItDown

from .auth import LoginRateLimiter
from .config import Settings, settings_from_overrides
from .extract import build_markitdown
from .line_client import LineClient
from .onedrive import OneDriveClient
from .settings_store import ConfigStore, ensure_oauth_setup_secret
from .store import Store

logger = logging.getLogger(__name__)

# Changing these fields requires rebuilding the named runtime object(s).
_OCR_FIELDS = {"ocr_backend", "anthropic_api_key", "gemini_api_key", "claude_model", "gemini_model"}
_LINE_CLIENT_FIELDS = {"line_channel_access_token"}
# ms_client_id/ms_redirect_uri/public_base_url/oauth_setup_secret together
# determine the MSAL client id and redirect URI the OneDriveClient uses --
# any of them changing means it (and the MSAL PublicClientApplication
# inside it) must be rebuilt for a Setup > OneDrive change to take effect
# without a restart. The on-disk MSAL token cache is re-read fresh on
# rebuild, so an existing sign-in survives unless ms_client_id itself
# changed to a different Entra app registration.
_ONEDRIVE_CLIENT_FIELDS = {"ms_client_id", "ms_redirect_uri", "public_base_url", "oauth_setup_secret"}
# Changing these fields cannot be hot-applied at all -- see AppState.apply_changes.
_RESTART_REQUIRED_FIELDS = {"setup_ui_exposure"}


@dataclass
class GroupDetectState:
    """"Detect group" flow state: while `listening_until` is in the future,
    the public webhook handler records the next group message it sees here.
    Detection only ever *records* an id -- it never affects which group's
    messages get filed (that's still settings.line_lab_group_id alone).
    """

    listening_until: Optional[float] = None
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    found_at: Optional[float] = None

    def start(self, duration_seconds: float = 120.0) -> None:
        self.listening_until = time.time() + duration_seconds
        self.group_id = None
        self.group_name = None
        self.found_at = None

    def is_listening(self) -> bool:
        return self.listening_until is not None and time.time() < self.listening_until

    def record(self, group_id: str, group_name: Optional[str]) -> None:
        self.group_id = group_id
        self.group_name = group_name
        self.found_at = time.time()
        self.listening_until = None  # stop listening once found


@dataclass
class AppState:
    base_settings: Settings  # the env/.env/default layer, fixed for the life of the process
    settings: Settings  # base_settings + current ConfigStore overrides -- what everything else should read
    config_store: ConfigStore
    store: Store
    line_client: LineClient
    markitdown: MarkItDown
    onedrive: OneDriveClient
    data_dir: Path
    queue: "Any" = None  # asyncio.Queue[dict], set by main.py after asyncio is running
    login_rate_limiter: LoginRateLimiter = field(default_factory=LoginRateLimiter)
    group_detect: GroupDetectState = field(default_factory=GroupDetectState)

    @classmethod
    def create(cls, base_settings: Settings) -> "AppState":
        """Build every shared runtime object once, at process startup.

        Every field Settings needs may be unset at this point -- nothing is
        required to boot (see app.config.Settings) -- so every runtime
        object built here must tolerate that (LineClient/OneDriveClient
        with an empty token/client id, MarkItDown with a placeholder OCR
        converter -- see `build_markitdown`). What's still missing is
        reported via `settings.missing_requirements()`, not a crash here.
        """
        base_settings.data_dir.mkdir(parents=True, exist_ok=True)
        config_store = ConfigStore(base_settings.data_dir / "linebot_lab.sqlite3", base_settings.data_dir)
        ensure_oauth_setup_secret(config_store, base_settings)  # first-boot generation, see that function's docstring
        settings = settings_from_overrides(config_store.overrides(), base=base_settings)
        store = Store(settings.data_dir / "linebot_lab.sqlite3")
        line_client = LineClient(settings.line_channel_access_token or "")
        markitdown = build_markitdown(settings)
        onedrive = OneDriveClient(settings.ms_client_id or "", settings.resolved_redirect_uri or "", settings.data_dir)

        return cls(
            base_settings=base_settings,
            settings=settings,
            config_store=config_store,
            store=store,
            line_client=line_client,
            markitdown=markitdown,
            onedrive=onedrive,
            data_dir=settings.data_dir,
        )

    # ---- hot reload ----

    def reload_settings(self) -> Settings:
        self.settings = settings_from_overrides(self.config_store.overrides(), base=self.base_settings)
        return self.settings

    def rebuild_ocr(self) -> None:
        """Re-instantiate MarkItDown and re-register the OCR converter at
        priority -1, in place, so a changed OCR_BACKEND/API key takes
        effect on the next photo without a restart."""
        self.markitdown = build_markitdown(self.settings)
        logger.info("OCR backend hot-reloaded: ocr_backend=%s", self.settings.ocr_backend)

    def rebuild_line_client(self) -> None:
        """Rebuild the LINE client with the new access token, in place."""
        self.line_client = LineClient(self.settings.line_channel_access_token or "")
        logger.info("LINE client hot-reloaded")

    def rebuild_onedrive(self) -> None:
        """Rebuild the OneDrive/MSAL client in place, so a Setup > OneDrive
        change to ms_client_id, public_base_url, an explicit ms_redirect_uri
        override, or a regenerated oauth_setup_secret takes effect
        immediately with no restart. The on-disk MSAL token cache
        (data/msal_cache.bin) is re-read fresh, so an existing sign-in
        survives the rebuild unless ms_client_id changed to a different
        Entra app registration (in which case re-signing in is expected).
        """
        self.onedrive = OneDriveClient(
            self.settings.ms_client_id or "",
            self.settings.resolved_redirect_uri or "",
            self.settings.data_dir,
        )
        logger.info("OneDrive/MSAL client hot-reloaded")

    def apply_changes(self, changed_fields: set[str]) -> list[str]:
        """After writing `changed_fields` to the ConfigStore, reload
        Settings and rebuild whatever that touches. Returns a list of
        human-readable notes for fields that could NOT be hot-applied (the
        UI shows these as a "restart required" banner) -- currently just
        SETUP_UI_EXPOSURE, since which routes are mounted on which port is
        decided once, when the FastAPI apps are constructed, and Starlette
        does not support safely unmounting routes from a running app.
        """
        self.reload_settings()

        if changed_fields & _OCR_FIELDS:
            self.rebuild_ocr()
        if changed_fields & _LINE_CLIENT_FIELDS:
            self.rebuild_line_client()
        if changed_fields & _ONEDRIVE_CLIENT_FIELDS:
            self.rebuild_onedrive()

        notes = []
        if changed_fields & _RESTART_REQUIRED_FIELDS:
            notes.append(
                "SETUP_UI_EXPOSURE changed -- restart the container for this to take effect "
                "(which port(s) the admin UI is mounted on is decided once, at startup)."
            )
        return notes

    # ---- convenience ----

    def effective_onedrive_root_path(self) -> str:
        """Best-effort synchronous fallback path for display purposes only
        (dashboard etc.) -- does NOT resolve a live item id; use
        `onedrive.resolve_item_path` for that where correctness matters."""
        return self.settings.onedrive_folder_path or self.settings.onedrive_root

    async def aclose(self) -> None:
        await self.line_client.aclose()
        await self.onedrive.aclose()
        self.store.close()
        self.config_store.close()
