"""Microsoft OneDrive (personal/consumer) integration.

Personal OneDrive does not support app-only/client-credentials auth -- it
requires a signed-in user and a refresh token (delegated auth). The one-time
consent happens via the bot's own routes (`/oauth/start`, `/oauth/callback`
in app/main.py, which call `start_auth`/`complete_auth` here) using MSAL's
auth-code flow with PKCE. The resulting refresh token lives in an MSAL
`SerializableTokenCache` persisted to `data/msal_cache.bin` (mode 0600) so
it survives container restarts; every upload goes through
`acquire_token_silent`, which uses the refresh token transparently.

Uploads target `/me/drive/root:/<path>:/content` (or a chunked upload
session above 4 MB), always with `@microsoft.graph.conflictBehavior=fail`.
A 409 means "name taken" -- the caller bumps the numeric sequence suffix and
retries. This is deliberately conflict-driven rather than list-then-write,
so two results landing at the same time cannot overwrite each other.
"""

from __future__ import annotations

import logging
import os
import stat
import urllib.parse
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx
import msal

logger = logging.getLogger(__name__)

# MSAL adds offline_access (and openid/profile) itself; passing it explicitly
# raises. Files.ReadWrite is the only delegated scope this app needs.
SCOPES = ["Files.ReadWrite"]

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
CHUNKED_UPLOAD_THRESHOLD = 4 * 1024 * 1024  # 4 MB
UPLOAD_CHUNK_SIZE = 320 * 1024 * 10  # 3,276,800 bytes -- a multiple of 320 KiB, per Graph's requirement
MAX_SEQUENCE = 50


class OneDriveAuthError(RuntimeError):
    """The stored refresh token is missing, expired, or revoked -- an admin
    must re-run /oauth/start."""


class OneDriveError(RuntimeError):
    """An unrecoverable Microsoft Graph API error."""


def quote_path(path: str) -> str:
    """URL-encode each segment of a OneDrive path, joined back with '/'."""
    return "/".join(urllib.parse.quote(part, safe="") for part in path.strip("/").split("/") if part)


def candidate_stem(base_stem: str, seq: int) -> str:
    """Filename stem for 1-based sequence `seq`: the first result of the day
    uses the bare stem; later ones get a numeric suffix.

    >>> candidate_stem("2026-09-03", 1)
    '2026-09-03'
    >>> candidate_stem("2026-09-03", 2)
    '2026-09-03_2'
    >>> candidate_stem("2026-09-03", 3)
    '2026-09-03_3'
    """
    if seq <= 1:
        return base_stem
    return f"{base_stem}_{seq}"


class OneDriveClient:
    """Delegated-auth Microsoft Graph client for personal OneDrive."""

    def __init__(self, client_id: str, redirect_uri: str, data_dir: Path):
        self._redirect_uri = redirect_uri
        self._cache_path = data_dir / "msal_cache.bin"
        self._cache = msal.SerializableTokenCache()
        if self._cache_path.exists():
            self._cache.deserialize(self._cache_path.read_text(encoding="utf-8"))

        self._app = msal.PublicClientApplication(
            client_id,
            authority="https://login.microsoftonline.com/consumers",
            token_cache=self._cache,
        )
        # Holds the code_verifier/state between /oauth/start and
        # /oauth/callback for the one in-progress setup flow.
        self._pending_flow: Optional[dict[str, Any]] = None
        self._http = httpx.AsyncClient(timeout=60.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- token cache persistence ----

    def _save_cache(self) -> None:
        if not self._cache.has_state_changed:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(self._cache.serialize(), encoding="utf-8")
        os.chmod(self._cache_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600

    # ---- one-time consent flow ----

    def start_auth(self) -> str:
        """Begin the PKCE auth-code flow. Returns the URL to send the
        operator's browser to."""
        flow = self._app.initiate_auth_code_flow(scopes=SCOPES, redirect_uri=self._redirect_uri)
        self._pending_flow = flow
        return flow["auth_uri"]

    def complete_auth(self, query_params: Mapping[str, str]) -> None:
        """Complete the flow started by start_auth(), using the query
        parameters Microsoft redirected back to /oauth/callback with."""
        if self._pending_flow is None:
            raise OneDriveAuthError("No OAuth flow in progress -- visit /oauth/start first.")
        result = self._app.acquire_token_by_auth_code_flow(self._pending_flow, dict(query_params))
        self._pending_flow = None
        if "access_token" not in result:
            raise OneDriveAuthError(f"OAuth callback failed: {result.get('error_description', result)}")
        self._save_cache()

    # ---- token acquisition ----

    def _acquire_token(self) -> str:
        accounts = self._app.get_accounts()
        if not accounts:
            raise OneDriveAuthError("No OneDrive account authorized yet -- visit /oauth/start.")
        result = self._app.acquire_token_silent(SCOPES, account=accounts[0])
        self._save_cache()
        if not result or "access_token" not in result:
            raise OneDriveAuthError("OneDrive refresh token is missing/expired/revoked -- re-run /oauth/start.")
        return result["access_token"]

    def is_authorized(self) -> bool:
        """Best-effort health check for /healthz -- True if a usable token
        can be acquired right now."""
        try:
            self._acquire_token()
            return True
        except OneDriveAuthError:
            return False

    # ---- folder creation ----

    async def ensure_folder_path(self, path: str) -> None:
        """Create every segment of `path` (relative to the drive root) in
        order, treating HTTP 409 (already exists) as success, so parents are
        created deterministically rather than relying on implicit creation.
        """
        segments = [s for s in path.strip("/").split("/") if s]
        built = ""
        for segment in segments:
            token = self._acquire_token()
            if built:
                url = f"{GRAPH_ROOT}/me/drive/root:/{quote_path(built)}:/children"
            else:
                url = f"{GRAPH_ROOT}/me/drive/root/children"
            resp = await self._http.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"name": segment, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
            )
            if resp.status_code not in (201, 409):
                raise OneDriveError(f"Failed to create folder '{built}/{segment}': {resp.status_code} {resp.text}")
            built = f"{built}/{segment}" if built else segment

    # ---- upload ----

    async def _put_small(self, path: str, content: bytes, conflict: str) -> httpx.Response:
        token = self._acquire_token()
        url = f"{GRAPH_ROOT}/me/drive/root:/{quote_path(path)}:/content"
        return await self._http.put(
            url,
            params={"@microsoft.graph.conflictBehavior": conflict},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
            content=content,
        )

    async def _put_large(self, path: str, content: bytes, conflict: str) -> httpx.Response:
        token = self._acquire_token()
        session_url = f"{GRAPH_ROOT}/me/drive/root:/{quote_path(path)}:/createUploadSession"
        resp = await self._http.post(
            session_url,
            headers={"Authorization": f"Bearer {token}"},
            json={"item": {"@microsoft.graph.conflictBehavior": conflict}},
        )
        if resp.status_code == 409:
            return resp
        resp.raise_for_status()
        upload_url = resp.json()["uploadUrl"]

        total = len(content)
        start = 0
        last_resp: Optional[httpx.Response] = None
        while start < total:
            end = min(start + UPLOAD_CHUNK_SIZE, total)
            chunk = content[start:end]
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end - 1}/{total}",
            }
            # IMPORTANT: no Authorization header here -- the upload session
            # URL is pre-authorized, and Graph can 401 if it's present.
            last_resp = await self._http.put(upload_url, headers=headers, content=chunk)
            if last_resp.status_code == 409:
                return last_resp
            last_resp.raise_for_status()
            start = end
        assert last_resp is not None
        return last_resp

    async def _upload(self, path: str, content: bytes, conflict: str = "fail") -> bool:
        """Upload `content` to `path`. Returns True on success, False on a
        409 name conflict (caller should retry under a different name),
        raises OneDriveError on any other failure.
        """
        if len(content) <= CHUNKED_UPLOAD_THRESHOLD:
            resp = await self._put_small(path, content, conflict)
        else:
            resp = await self._put_large(path, content, conflict)

        if resp.status_code == 409:
            return False
        if resp.status_code not in (200, 201):
            raise OneDriveError(f"Upload failed for '{path}': {resp.status_code} {resp.text}")
        return True

    async def upload_pair(
        self,
        folder_path: str,
        base_stem: str,
        jpg_bytes: bytes,
        md_text: str,
        jpg_ext: str = "jpg",
    ) -> tuple[str, str]:
        """Upload the photo and its .md transcript into `folder_path`,
        sharing one filename stem. On a 409 name conflict, bump the numeric
        sequence suffix and retry both files under the new name, up to
        MAX_SEQUENCE times.
        """
        await self.ensure_folder_path(folder_path)
        md_bytes = md_text.encode("utf-8")

        for seq in range(1, MAX_SEQUENCE + 1):
            stem = candidate_stem(base_stem, seq)
            jpg_path = f"{folder_path}/{stem}.{jpg_ext}"
            if not await self._upload(jpg_path, jpg_bytes):
                continue  # jpg name taken -- try the next sequence number

            md_path = f"{folder_path}/{stem}.md"
            if await self._upload(md_path, md_bytes):
                return jpg_path, md_path
            # Extremely unlikely: the jpg claimed the name but the .md
            # didn't. Keep bumping the sequence for both files together.

        raise OneDriveError(f"Exhausted {MAX_SEQUENCE} filename sequence attempts under '{folder_path}/{base_stem}'")
