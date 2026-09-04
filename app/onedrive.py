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
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._cache_path = data_dir / "msal_cache.bin"
        self._cache = msal.SerializableTokenCache()
        if self._cache_path.exists():
            self._cache.deserialize(self._cache_path.read_text(encoding="utf-8"))

        # The MSAL PublicClientApplication is built lazily -- see `_app`
        # below -- NOT here in __init__, which runs unconditionally from
        # `AppState.create()` at process startup regardless of whether
        # OneDrive is configured at all. Nothing is required to boot (see
        # app.config.Settings); building it eagerly would silently violate
        # that, since MSAL's Authority always performs a live network call
        # (OIDC tenant discovery) inside its own constructor -- there is no
        # supported way to skip it, even for a fixed, well-known authority
        # like this one -- so a DNS hiccup or a not-yet-up network link at
        # boot would otherwise take the whole process down before it ever
        # served a request.
        self._app_instance: Optional[msal.PublicClientApplication] = None
        # Holds the code_verifier/state between /oauth/start and
        # /oauth/callback for the one in-progress setup flow.
        self._pending_flow: Optional[dict[str, Any]] = None
        self._http = httpx.AsyncClient(timeout=60.0)

    @property
    def _app(self) -> msal.PublicClientApplication:
        """Build (once) and return the MSAL PublicClientApplication. See the
        docstring in __init__ for why this is lazy rather than eager."""
        if self._app_instance is None:
            self._app_instance = msal.PublicClientApplication(
                self._client_id,
                authority="https://login.microsoftonline.com/consumers",
                token_cache=self._cache,
            )
        return self._app_instance

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

    def start_auth(self, state: Optional[str] = None) -> str:
        """Begin the PKCE auth-code flow. Returns the URL to send the
        operator's browser to.

        `state` is round-tripped by Microsoft back to the redirect URI
        untouched. The caller passes the anti-hijack setup secret through it
        (see `app.main._oauth_state_value`), because the redirect URI itself
        cannot carry a query string for personal-Microsoft-account app
        registrations. Passing it here rather than letting MSAL generate a
        random one keeps MSAL's own CSRF check intact: MSAL stores this value
        on the flow and `acquire_token_by_auth_code_flow` rejects a callback
        whose state does not match it.
        """
        flow = self._app.initiate_auth_code_flow(
            scopes=SCOPES, redirect_uri=self._redirect_uri, state=state
        )
        self._pending_flow = flow
        return flow["auth_uri"]

    def complete_auth(self, query_params: Mapping[str, str]) -> None:
        """Complete the flow started by start_auth(), using the query
        parameters Microsoft redirected back to /oauth/callback with."""
        if self._pending_flow is None:
            raise OneDriveAuthError("No OAuth flow in progress -- visit /oauth/start first.")
        try:
            result = self._app.acquire_token_by_auth_code_flow(self._pending_flow, dict(query_params))
        except ValueError as exc:
            # MSAL raises a bare ValueError when the returned state does not
            # match the one pinned to the pending flow (a replayed or
            # tampered callback). Without this it would escape /oauth/callback
            # as a 500; it is a bad request, not a server fault.
            raise OneDriveAuthError(f'OAuth callback rejected: {exc}') from exc
        finally:
            self._pending_flow = None
        if "access_token" not in result:
            raise OneDriveAuthError(f"OAuth callback failed: {result.get('error_description', result)}")
        self._save_cache()

    # ---- token acquisition ----

    def _acquire_token(self) -> str:
        try:
            app = self._app  # first touch may build the MSAL client -- see _app's docstring
        except Exception as exc:
            # Most likely a network/DNS failure reaching Microsoft's login
            # endpoint (see _app's docstring) -- never let that surface as
            # anything other than "not authorized right now" to callers like
            # is_authorized()/healthz/the dashboard, which must never crash
            # on this.
            raise OneDriveAuthError(f"OneDrive/MSAL client is not reachable right now: {exc}") from exc
        accounts = app.get_accounts()
        if not accounts:
            raise OneDriveAuthError("No OneDrive account authorized yet -- visit /oauth/start.")
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
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

    # ---- account / connection state ----

    def get_account_info(self) -> Optional[str]:
        """Best-effort: the signed-in account's username (usually an email
        address), or None if not connected -- also None (rather than
        raising) if the MSAL client can't be built right now (see `_app`'s
        docstring: its first build, whenever that happens, needs the
        network). Called from the admin dashboard on every page load, which
        must never 500 over this."""
        try:
            accounts = self._app.get_accounts()
        except Exception:
            logger.warning("Could not read OneDrive account info (OneDrive/MSAL client unreachable)", exc_info=True)
            return None
        if not accounts:
            return None
        return accounts[0].get("username")

    def disconnect(self) -> None:
        """Forget the signed-in account. A subsequent upload/health check
        will report unauthorized until /oauth/start is repeated."""
        for account in list(self._app.get_accounts()):
            self._app.remove_account(account)
        self._save_cache()
        if self._cache_path.exists():
            self._cache_path.unlink()

    # ---- folder browsing (setup UI folder picker) ----

    def _anchor(self, root_item_id: Optional[str]) -> str:
        if root_item_id:
            return f"/me/drive/items/{root_item_id}"
        return "/me/drive/root"

    async def resolve_item_path(self, item_id: str) -> str:
        """Return the drive-root-relative path of `item_id`, e.g.
        '/LabResults/Custom Folder', by resolving it fresh from Graph. Used
        so that filing by a stored item id keeps working after the folder
        is renamed/moved in OneDrive -- every use re-resolves the current
        path right before it's needed, rather than trusting a cached one.
        """
        token = self._acquire_token()
        resp = await self._http.get(
            f"{GRAPH_ROOT}/me/drive/items/{item_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            raise OneDriveError(f"Could not resolve OneDrive item '{item_id}': {resp.status_code} {resp.text}")
        data = resp.json()
        parent = data.get("parentReference") or {}
        parent_path = parent.get("path", "")  # e.g. '/drive/root:/LabResults' or '/drive/root:'
        if ":" in parent_path:
            parent_path = parent_path.split(":", 1)[1]
        name = data.get("name", "")
        return f"{parent_path}/{name}".replace("//", "/") if name else parent_path or "/"

    async def get_item_by_path(self, path: str) -> dict[str, Any]:
        """Fetch an item's metadata (id, name, parentReference, ...) by its
        drive-root-relative path."""
        token = self._acquire_token()
        resp = await self._http.get(
            f"{GRAPH_ROOT}/me/drive/root:/{quote_path(path)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            raise OneDriveError(f"OneDrive item not found at '{path}': {resp.status_code} {resp.text}")
        return resp.json()

    async def delete_item(self, *, path: str) -> bool:
        """Delete the item at drive-root-relative `path`.

        Returns True if it was deleted, False if there was nothing there
        (404) -- an already-deleted file is not an error for callers that
        just want it gone. Any other non-success status raises.

        Graph moves the item to the OneDrive recycle bin rather than erasing
        it, so this stays recoverable for the account's retention window.
        That matters here: the files being deleted are patient records, and
        "dismissed by mistake" needs to be undoable from OneDrive's own UI.
        """
        token = self._acquire_token()
        resp = await self._http.delete(
            f"{GRAPH_ROOT}/me/drive/root:/{quote_path(path)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 404:
            logger.info("Nothing to delete at '%s' (already gone)", path)
            return False
        if resp.status_code not in (200, 204):
            raise OneDriveError(
                f"Could not delete OneDrive item '{path}': {resp.status_code} {resp.text}"
            )
        return True

    async def list_children(self, item_id: Optional[str] = None) -> list[dict[str, Any]]:
        """List the folder-only children of `item_id` (or the drive root if
        None), for the setup UI's folder picker. Sorted by name."""
        token = self._acquire_token()
        anchor = self._anchor(item_id)
        resp = await self._http.get(
            f"{GRAPH_ROOT}{anchor}/children",
            headers={"Authorization": f"Bearer {token}"},
            params={"$select": "id,name,folder", "$top": "999"},
        )
        if resp.status_code != 200:
            raise OneDriveError(f"Failed to list OneDrive folder children: {resp.status_code} {resp.text}")
        items = resp.json().get("value", [])
        folders = [item for item in items if "folder" in item]
        folders.sort(key=lambda item: item.get("name", "").lower())
        return folders

    async def create_folder(self, parent_item_id: Optional[str], name: str) -> dict[str, Any]:
        """Create a new folder under `parent_item_id` (or the drive root if
        None). Raises OneDriveError (including on a 409 name clash -- the
        caller decides what a duplicate name means in the picker UI)."""
        token = self._acquire_token()
        anchor = self._anchor(parent_item_id)
        resp = await self._http.post(
            f"{GRAPH_ROOT}{anchor}/children",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        )
        if resp.status_code == 409:
            raise OneDriveError(f"A folder named '{name}' already exists here.")
        if resp.status_code != 201:
            raise OneDriveError(f"Failed to create folder '{name}': {resp.status_code} {resp.text}")
        return resp.json()

    # ---- move (unfiled-queue resolution) ----

    async def _move(self, item_id: str, new_parent_id: str, new_name: str) -> bool:
        """PATCH-move `item_id` into `new_parent_id` under `new_name`.
        Returns True on success, False on a 409 name conflict (caller
        should bump the sequence suffix and retry), raises OneDriveError on
        any other failure.
        """
        token = self._acquire_token()
        resp = await self._http.patch(
            f"{GRAPH_ROOT}/me/drive/items/{item_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"parentReference": {"id": new_parent_id}, "name": new_name},
        )
        if resp.status_code == 409:
            return False
        if resp.status_code not in (200, 201):
            raise OneDriveError(f"Failed to move item '{item_id}': {resp.status_code} {resp.text}")
        return True

    async def move_pair(
        self,
        jpg_path: str,
        md_path: str,
        dest_folder_path: str,
        base_stem: str,
        *,
        root_item_id: Optional[str] = None,
        jpg_ext: str = "jpg",
    ) -> tuple[str, str]:
        """Move the photo and its `.md` (currently at `jpg_path`/`md_path`)
        into `dest_folder_path`, sharing one filename stem, reusing the same
        sequence-suffix-on-409 logic as `upload_pair`. Returns the new
        (jpg_path, md_path). Raises OneDriveError -- and moves nothing
        further -- on any failure; the caller must NOT mark the source
        resolved when this raises.
        """
        jpg_item = await self.get_item_by_path(jpg_path)
        md_item = await self.get_item_by_path(md_path)
        dest_folder_id = await self.ensure_folder_path(dest_folder_path, root_item_id=root_item_id)

        for seq in range(1, MAX_SEQUENCE + 1):
            stem = candidate_stem(base_stem, seq)
            jpg_name = f"{stem}.{jpg_ext}"
            if not await self._move(jpg_item["id"], dest_folder_id, jpg_name):
                continue  # name taken -- try the next sequence number

            md_name = f"{stem}.md"
            if await self._move(md_item["id"], dest_folder_id, md_name):
                return f"{dest_folder_path}/{jpg_name}", f"{dest_folder_path}/{md_name}"
            # jpg moved but md's name collided -- keep bumping together.

        raise OneDriveError(f"Exhausted {MAX_SEQUENCE} filename sequence attempts moving into '{dest_folder_path}/{base_stem}'")

    # ---- content download (unfiled-queue photo preview proxy) ----

    async def download_bytes(self, *, item_id: Optional[str] = None, path: Optional[str] = None) -> bytes:
        """Fetch raw file content, by item id or by drive-root-relative
        path (exactly one of the two). Used to proxy a photo preview to the
        admin UI without ever handing the browser a OneDrive link or token.
        """
        if bool(item_id) == bool(path):
            raise ValueError("download_bytes requires exactly one of item_id or path")
        token = self._acquire_token()
        if item_id:
            url = f"{GRAPH_ROOT}/me/drive/items/{item_id}/content"
        else:
            url = f"{GRAPH_ROOT}/me/drive/root:/{quote_path(path)}:/content"
        resp = await self._http.get(url, headers={"Authorization": f"Bearer {token}"}, follow_redirects=True)
        if resp.status_code != 200:
            raise OneDriveError(f"Failed to download OneDrive content: {resp.status_code} {resp.text}")
        return resp.content

    # ---- folder creation ----

    async def ensure_folder_path(self, path: str, root_item_id: Optional[str] = None) -> str:
        """Create every segment of `path` (relative to `root_item_id`, or
        the drive root if None) in order, treating HTTP 409 (already
        exists) as success, so parents are created deterministically rather
        than relying on implicit creation. Returns the item id of the final
        (deepest) folder.
        """
        segments = [s for s in path.strip("/").split("/") if s]
        anchor = self._anchor(root_item_id)
        built = ""
        current_id = root_item_id

        for segment in segments:
            token = self._acquire_token()
            if built:
                url = f"{GRAPH_ROOT}{anchor}:/{quote_path(built)}:/children"
            else:
                url = f"{GRAPH_ROOT}{anchor}/children"
            resp = await self._http.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"name": segment, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
            )
            built = f"{built}/{segment}" if built else segment
            if resp.status_code == 201:
                current_id = resp.json()["id"]
            elif resp.status_code == 409:
                token = self._acquire_token()
                get_resp = await self._http.get(
                    f"{GRAPH_ROOT}{anchor}:/{quote_path(built)}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if get_resp.status_code != 200:
                    raise OneDriveError(f"Failed to resolve existing folder '{built}': {get_resp.status_code} {get_resp.text}")
                current_id = get_resp.json()["id"]
            else:
                raise OneDriveError(f"Failed to create folder '{built}': {resp.status_code} {resp.text}")

        if current_id is None:
            # No segments (path was root) and no root_item_id given -- look
            # up the actual drive root's id.
            token = self._acquire_token()
            resp = await self._http.get(f"{GRAPH_ROOT}/me/drive/root", headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            current_id = resp.json()["id"]
        return current_id

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
