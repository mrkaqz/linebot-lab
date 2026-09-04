"""Application configuration, read from the environment / .env file.

All settings are read through a single `Settings` instance obtained via
`get_settings()`. Nothing here should ever be logged at INFO with its raw
value -- secrets are marked below and callers must redact them explicitly
when logging (see `app.main` startup logging).

**Nothing is required to boot.** Every field below has a default (or is
Optional with default=None), so `Settings()` constructs successfully against
a completely empty environment -- the container comes up "unconfigured" and
the admin UI (port 8001) walks the operator through finishing setup in the
browser. `missing_requirements()` is the single source of truth for what's
still missing, grouped by capability, used by both the admin dashboard's
setup checklist and `/healthz`'s machine-readable status.
"""

from __future__ import annotations

import urllib.parse
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Capability group keys used by missing_requirements() / the admin UI's
# setup checklist -- kept as constants so the admin router and templates
# don't have to guess the spelling.
GROUP_LINE = "line"
GROUP_ONEDRIVE = "onedrive"
GROUP_OCR = "ocr"


def _parse_public_base_url(value: str) -> str:
    """Validate/normalise a public base URL: absolute http(s), has a host,
    no trailing slash. Raises ValueError on anything else. Shared by the
    Settings field validator and the admin router (which validates a
    submitted value before writing it to the DB, so a bad entry is rejected
    with a clear message instead of silently breaking OAuth later).
    """
    stripped = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(stripped)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            "must be an absolute http(s) URL with a host, e.g. "
            f"https://abc123.trycloudflare.com (got {value!r})"
        )
    return stripped


class Settings(BaseSettings):
    """Environment-driven configuration.

    Field names map to environment variables of the same name, upper-cased
    (pydantic-settings default). See .env.example for documentation of every
    field and a realistic sample value.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LINE ---
    line_channel_secret: Optional[str] = Field(
        default=None, description="LINE channel secret, used to verify webhook signatures."
    )
    line_channel_access_token: Optional[str] = Field(
        default=None, description="LINE channel access token, used to call the Messaging API."
    )
    line_lab_group_id: Optional[str] = Field(
        default=None,
        description="The LINE group id the lab posts into. If unset, the bot processes nothing (fail safe).",
    )
    admin_line_id: Optional[str] = Field(
        default=None,
        description="A LINE user id to push admin notifications to (unfiled results, OneDrive auth expiry). Optional.",
    )

    # --- OCR backend selection ---
    ocr_backend: Literal["claude", "gemini", "tesseract"] = Field(
        default="tesseract",
        description="Which OCR/extraction backend to plug into MarkItDown.",
    )
    claude_model: str = Field(default="claude-opus-5", description="Anthropic model id, used only when ocr_backend=claude.")
    anthropic_api_key: Optional[str] = Field(default=None, description="Anthropic API key, required when ocr_backend=claude.")
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        description="Google Gemini model id, used only when ocr_backend=gemini. List valid ids with client.models.list().",
    )
    gemini_api_key: Optional[str] = Field(default=None, description="Google Gemini API key, required when ocr_backend=gemini.")

    # --- OPD extraction ---
    opd_regex: str = Field(
        default=r"(?i)\b(?:OPD|O\.?P\.?D\.?|HN)\s*(?:no\.?|number|เลขที่)?\s*[:.#\-]?\s*([0-9][0-9\-/]{3,})",
        description="Regex applied to the extracted markdown to find/cross-check the OPD number. Group 1 is the captured number.",
    )

    # --- Filing ---
    timezone: str = Field(default="Asia/Bangkok", description="IANA timezone used to compute the 'received date' from the LINE event timestamp.")
    onedrive_root: str = Field(default="/LabResults", description="Root folder path in OneDrive under which OPD-numbered subfolders are created.")
    onedrive_folder_id: Optional[str] = Field(
        default=None,
        description="OneDrive item id of the root filing folder, chosen via the admin UI's folder picker. Takes precedence over ONEDRIVE_ROOT for addressing (a rename in OneDrive doesn't break filing); ONEDRIVE_ROOT remains the path shown/used when this is unset.",
    )
    onedrive_folder_path: Optional[str] = Field(
        default=None,
        description="Human-readable path of onedrive_folder_id, stored alongside it for display only -- never used for addressing.",
    )

    # --- Microsoft / OneDrive OAuth ---
    ms_client_id: Optional[str] = Field(
        default=None, description="Entra ID (Azure AD) application (client) ID, registered for personal Microsoft accounts."
    )
    public_base_url: Optional[str] = Field(
        default=None,
        description=(
            "This service's public HTTPS base URL (e.g. https://abc123.trycloudflare.com, no trailing "
            "slash) -- the app derives the OAuth redirect URI ({public_base_url}/oauth/callback?secret=...) "
            "and the LINE webhook URL ({public_base_url}/line/webhook) from it. See ms_redirect_uri for an "
            "explicit override of the derived redirect URI."
        ),
    )
    ms_redirect_uri: Optional[str] = Field(
        default=None,
        description=(
            "Explicit override for the OAuth redirect URI, taking precedence over the one derived from "
            "public_base_url. Must exactly match a redirect URI registered on the Entra app, e.g. "
            "https://<tunnel-host>/oauth/callback?secret=<oauth_setup_secret>. Only needed if the derived "
            "value (public_base_url + /oauth/callback?secret=...) isn't right for your setup."
        ),
    )
    oauth_setup_secret: Optional[str] = Field(
        default=None,
        description=(
            "Shared secret required as a query param on /oauth/start and /oauth/callback, so a stranger who "
            "finds the public tunnel URL can't authorize the bot against their own OneDrive. Auto-generated "
            "and persisted on first boot if left unset here -- see app.settings_store.ensure_oauth_setup_secret. "
            "An explicit value here always wins over a generated one."
        ),
    )

    # --- Storage / misc ---
    data_dir: Path = Field(default=Path("data"), description="Directory for the SQLite database and the MSAL token cache.")
    log_level: str = Field(default="INFO", description="Python logging level name.")

    # --- Web admin UI ---
    setup_ui_exposure: Literal["lan", "public"] = Field(
        default="lan",
        description=(
            "'lan' (default): the admin UI (port 8001) is the only place it's mounted -- a request to the "
            "public tunnel URL (port 8000) for an admin route gets a plain 404, not a login page. 'public': the "
            "admin UI is ALSO mounted on the public port, for setups with no LAN access to the Pi. Authentication "
            "is required in both modes either way; this only controls whether the login page is reachable "
            "from the internet at all. Changing this setting requires a restart to take effect."
        ),
    )

    @field_validator(
        "line_channel_secret", "line_channel_access_token", "line_lab_group_id", "admin_line_id",
        "anthropic_api_key", "gemini_api_key", "onedrive_folder_id", "onedrive_folder_path",
        "ms_client_id", "public_base_url", "ms_redirect_uri", "oauth_setup_secret",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Treat an empty-string env var the same as an unset one."""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("public_base_url")
    @classmethod
    def _validate_public_base_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _parse_public_base_url(value)

    # ---- derived OAuth/webhook URLs ----

    @property
    def resolved_redirect_uri(self) -> Optional[str]:
        """The OAuth redirect URI actually in effect: `ms_redirect_uri` (an
        explicit override) wins when set; otherwise it's derived from
        `public_base_url` + `oauth_setup_secret`. None if neither source is
        configured yet.
        """
        if self.ms_redirect_uri:
            return self.ms_redirect_uri
        if self.public_base_url and self.oauth_setup_secret:
            return f"{self.public_base_url}/oauth/callback?secret={self.oauth_setup_secret}"
        return None

    @property
    def line_webhook_url(self) -> Optional[str]:
        """The LINE webhook URL derived from `public_base_url`, or None if
        it isn't set yet."""
        if not self.public_base_url:
            return None
        return f"{self.public_base_url}/line/webhook"

    # ---- configuration status ----

    def missing_requirements(self, *, onedrive_authorized: Optional[bool] = None) -> dict[str, list[str]]:
        """Report which capabilities are not yet fully configured, grouped
        by capability key (GROUP_LINE / GROUP_ONEDRIVE / GROUP_OCR) so the
        admin UI can render a setup checklist and /healthz can report a
        machine-readable status. A group is present only when something in
        it is still missing; an empty dict means everything this method can
        see is configured.

        `onedrive_authorized` is the live sign-in state from
        `OneDriveClient.is_authorized()`, which isn't knowable from Settings
        alone -- pass it (from the admin UI / /healthz, which have a live
        OneDriveClient at hand) to also report a still-needed OneDrive
        sign-in. Omit it (None) to report only the Settings-derived
        requirements (client id + public base URL), which is all a plain
        Settings instance -- e.g. in a test -- can know about.
        """
        missing: dict[str, list[str]] = {}

        line: list[str] = []
        if not self.line_channel_secret:
            line.append("LINE channel secret")
        if not self.line_channel_access_token:
            line.append("LINE channel access token")
        if not self.line_lab_group_id:
            line.append("LINE lab group id")
        if line:
            missing[GROUP_LINE] = line

        onedrive: list[str] = []
        if not self.ms_client_id:
            onedrive.append("Microsoft Entra application (client) ID")
        if not self.public_base_url and not self.ms_redirect_uri:
            onedrive.append("Public base URL")
        if onedrive_authorized is False:
            onedrive.append("OneDrive sign-in")
        if onedrive:
            missing[GROUP_ONEDRIVE] = onedrive

        ocr: list[str] = []
        if self.ocr_backend == "claude" and not self.anthropic_api_key:
            ocr.append("Anthropic API key")
        if self.ocr_backend == "gemini" and not self.gemini_api_key:
            ocr.append("Gemini API key")
        if ocr:
            missing[GROUP_OCR] = ocr

        return missing

    def require_backend_credentials(self) -> None:
        """Raise if the selected OCR backend is missing its credentials.

        NOT called at startup any more -- a misconfigured/unconfigured OCR
        backend is reported via `missing_requirements()` (a WARNING at
        startup, a checklist item in the admin UI, a `/healthz` field), not
        a boot-time crash; see app.main.run() and app.extract.build_markitdown.
        This method remains for on-demand validation -- e.g. the admin UI's
        Setup > OCR > "Test backend" action, which deliberately wants an
        immediate, specific error for the backend/key combination it's
        about to try.
        """
        if self.ocr_backend == "claude" and not self.anthropic_api_key:
            raise RuntimeError(
                "OCR_BACKEND=claude requires ANTHROPIC_API_KEY to be set."
            )
        if self.ocr_backend == "gemini" and not self.gemini_api_key:
            raise RuntimeError(
                "OCR_BACKEND=gemini requires GEMINI_API_KEY to be set."
            )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance (cached after first call),
    built purely from the environment/.env -- no DB layer. Used by tests,
    scripts, and anywhere a DB-backed ConfigStore isn't available/relevant.
    For the app's actual runtime settings (DB overrides applied), see
    `app.runtime.AppState.reload_settings`.
    """
    return Settings()  # type: ignore[call-arg]


def settings_from_overrides(overrides: dict[str, str], base: Optional[Settings] = None) -> Settings:
    """Build a Settings instance layering `overrides` (typically
    `ConfigStore.overrides()`, i.e. DB-set values) on top of `base`.

    When `base` is given (the normal runtime path -- see
    `app.runtime.AppState`), every field of `base` is passed through
    explicitly alongside `overrides`, then re-validated -- this pins the
    result to `base`'s already-resolved env/.env/default values for every
    field `overrides` doesn't touch, WITHOUT re-reading the environment (so
    it stays hermetic under tests that build `base` from explicit kwargs,
    and doesn't care whether the environment has changed since `base` was
    built). `overrides` wins for any field present in both, since explicit
    keyword arguments take precedence over Settings' other sources.

    When `base` is omitted, `overrides` is layered directly over the
    process environment/.env/defaults via the normal Settings() lookup --
    used the first time a ConfigStore is read, before any base Settings
    exists yet.
    """
    if base is not None:
        merged = {**base.model_dump(mode="python"), **overrides}
        return Settings(**merged)  # type: ignore[arg-type]
    return Settings(**overrides)  # type: ignore[arg-type]
