"""Application configuration, read from the environment / .env file.

All settings are read through a single `Settings` instance obtained via
`get_settings()`. Nothing here should ever be logged at INFO with its raw
value -- secrets are marked below and callers must redact them explicitly
when logging (see `app.main` startup logging).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    line_channel_secret: str = Field(..., description="LINE channel secret, used to verify webhook signatures.")
    line_channel_access_token: str = Field(..., description="LINE channel access token, used to call the Messaging API.")
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

    # --- Microsoft / OneDrive OAuth ---
    ms_client_id: str = Field(..., description="Entra ID (Azure AD) application (client) ID, registered for personal Microsoft accounts.")
    ms_redirect_uri: str = Field(..., description="Redirect URI registered on the Entra app, e.g. https://<tunnel-host>/oauth/callback.")
    oauth_setup_secret: str = Field(..., description="Shared secret required as a query param on /oauth/start and /oauth/callback.")

    # --- Storage / misc ---
    data_dir: Path = Field(default=Path("data"), description="Directory for the SQLite database and the MSAL token cache.")
    log_level: str = Field(default="INFO", description="Python logging level name.")

    @field_validator("line_lab_group_id", "admin_line_id", "anthropic_api_key", "gemini_api_key", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Treat an empty-string env var the same as an unset one."""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    def require_backend_credentials(self) -> None:
        """Fail loudly at startup if the selected OCR backend is missing its
        credentials. Called once during FastAPI startup so a misconfiguration
        is caught before the first lab result arrives, not on first use.
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
    """Return the process-wide Settings instance (cached after first call)."""
    return Settings()  # type: ignore[call-arg]
