"""Entrypoint: two FastAPI apps sharing one process and one `AppState`.

- `public_app` (port 8000): `/line/webhook`, `/oauth/callback`, `/oauth/start`,
  `/healthz` ONLY. This is what cloudflared forwards to the internet.
- `admin_app` (port 8001): the whole admin UI (`/`, `/setup/*`, `/unfiled`,
  `/login`, ...). Published to the LAN only -- cloudflared must NOT forward
  this port (see docker-compose.yml).

Both apps are mounted with the SAME `AppState` (`app.state.rt`) -- the
store, queue, OneDrive/LINE clients, MarkItDown, and current Settings are
built ONCE before either uvicorn server starts, not once per app. A
background worker task drains the shared queue for the life of the process.

`SETUP_UI_EXPOSURE=public` additionally mounts the admin router on
`public_app`; in the default `lan` mode it is not mounted there at all, so
an admin route hit on the tunnel URL 404s rather than serving a login page.
Because Starlette does not support safely unmounting routes from an app
that's already serving traffic, switching this setting takes effect only on
restart (see app/runtime.py `AppState.apply_changes`).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .admin.router import router as admin_router
from .auth import ensure_admin_password
from .config import Settings, get_settings
from .crypto import load_or_create_session_secret
from .line_client import verify_signature
from .onedrive import OneDriveAuthError, OneDriveClient
from .pipeline import process_image_event
from .runtime import AppState
from .store import Store

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _worker(state: AppState) -> None:
    """Drains the shared queue for the life of the process. Reads
    `state.settings`/`state.line_client`/`state.markitdown`/`state.onedrive`
    fresh on every iteration (not captured once at task creation) so a
    config change hot-applied via the admin UI is picked up by the very
    next event, with no restart.
    """
    while True:
        event = await state.queue.get()
        try:
            await process_image_event(
                event,
                settings=state.settings,
                line_client=state.line_client,
                markitdown=state.markitdown,
                onedrive=state.onedrive,
                store=state.store,
            )
        except Exception:
            logger.exception("Unhandled error processing event %s", event.get("message", {}).get("id"))
        finally:
            state.queue.task_done()


def _attach_state(app: FastAPI, state: AppState) -> None:
    app.state.rt = state


def _require_setup_secret(request: Request, settings: Settings) -> None:
    """Guard /oauth/start and /oauth/callback with a shared secret in the
    query string, so a public tunnel URL cannot be hijacked by a stranger
    into authorizing the bot against *their* OneDrive account. See
    README.md for how the redirect URI (Settings.resolved_redirect_uri)
    carries this secret so it survives Microsoft's redirect back to
    /oauth/callback.

    In practice `settings.oauth_setup_secret` is always set by the time
    this runs -- one is auto-generated on first boot if not provided via
    env (see `app.settings_store.ensure_oauth_setup_secret`) -- but this
    guards defensively anyway: a request arriving before that has happened,
    or against a Settings built directly without going through AppState
    (e.g. a test), must be rejected rather than crash inside
    `hmac.compare_digest` on a None secret.
    """
    configured_secret = settings.oauth_setup_secret
    if not configured_secret:
        raise HTTPException(status_code=403, detail="OAuth setup secret is not configured yet")
    secret = request.query_params.get("secret", "")
    if not hmac.compare_digest(secret, configured_secret):
        raise HTTPException(status_code=403, detail="missing or invalid setup secret")


def build_public_app(state: AppState) -> FastAPI:
    app = FastAPI(title="linebot-lab (public)")
    _attach_state(app, state)
    app.add_middleware(
        SessionMiddleware,
        secret_key=load_or_create_session_secret(state.data_dir),
        https_only=(state.settings.setup_ui_exposure == "public"),
        same_site="lax",
    )
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        """Liveness probe -- ALWAYS returns 200 (the compose/Portainer
        healthcheck depends on that even while unconfigured; distinguishing
        "container is up" from "fully set up" is what `configured`/`missing`
        are for, not the HTTP status).
        """
        rt: AppState = request.app.state.rt
        onedrive_ok = rt.onedrive.is_authorized()
        missing = rt.settings.missing_requirements(onedrive_authorized=onedrive_ok)
        configured = not missing
        return {
            "status": "ok" if configured else "degraded",
            "configured": configured,
            "missing": missing,
            "onedrive_authorized": onedrive_ok,
            "line_lab_group_id_configured": bool(rt.settings.line_lab_group_id),
            "queue_depth": rt.queue.qsize() if rt.queue is not None else 0,
        }

    @app.post("/line/webhook")
    async def line_webhook(request: Request) -> Response:
        rt: AppState = request.app.state.rt
        settings = rt.settings

        # Read the RAW body before any JSON parsing -- re-serializing the
        # parsed JSON would change the bytes and break the signature check.
        body = await request.body()
        signature = request.headers.get("x-line-signature", "")

        if not settings.line_channel_secret:
            # Unconfigured, not merely "no signature": log and 200 without
            # touching the payload at all. Never falls through to signature
            # verification with an empty/None secret -- that would make
            # verify_signature() trivially satisfiable by an empty header
            # instead of safely rejecting everything.
            logger.warning("Rejected webhook: LINE channel secret is not configured yet (Setup > LINE)")
            return Response(status_code=200)

        if not verify_signature(settings.line_channel_secret, body, signature):
            logger.warning("Rejected webhook with invalid x-line-signature")
            raise HTTPException(status_code=400, detail="invalid signature")

        payload = await request.json()

        for event in payload.get("events", []):
            source = event.get("source", {})
            group_id = source.get("groupId")

            if event.get("type") == "message":
                # The group id is not visible anywhere in the LINE console --
                # this is how an operator finds it to put in .env, and how
                # the admin UI's "Detect group" flow finds it too.
                logger.info("Message event seen: groupId=%s messageType=%s", group_id, event.get("message", {}).get("type"))

                # "Detect group" listening mode: record the id (and, best
                # effort, the display name) of the next message from ANY
                # group -- purely observational, never bypasses the
                # signature check above, and never causes anything to be
                # FILED from an unconfigured group (the filing gate below is
                # untouched by this).
                if group_id and rt.group_detect.is_listening():
                    rt.group_detect.record(group_id, None)
                    asyncio.create_task(_fetch_group_name(rt, group_id))

            if not settings.line_lab_group_id:
                continue  # fail safe: process nothing until a group id is configured

            if event.get("type") != "message":
                continue
            message = event.get("message", {})
            if message.get("type") != "image":
                continue
            if group_id != settings.line_lab_group_id:
                continue

            message_id = message.get("id")
            if not message_id:
                continue

            # Idempotency guard BEFORE any work: a LINE retry of an
            # already-processed message must not cause a second upload.
            if not rt.store.mark_processed(message_id):
                continue

            await rt.queue.put(event)

        return Response(status_code=200)

    @app.get("/oauth/start")
    async def oauth_start(request: Request) -> RedirectResponse:
        rt: AppState = request.app.state.rt
        _require_setup_secret(request, rt.settings)
        auth_url = rt.onedrive.start_auth()
        return RedirectResponse(auth_url)

    @app.get("/oauth/callback")
    async def oauth_callback(request: Request) -> dict:
        rt: AppState = request.app.state.rt
        _require_setup_secret(request, rt.settings)
        try:
            rt.onedrive.complete_auth(request.query_params)
        except OneDriveAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "detail": "OneDrive authorization complete."}

    if state.settings.setup_ui_exposure == "public":
        app.include_router(admin_router)

    return app


async def _fetch_group_name(state: AppState, group_id: str) -> None:
    """Best-effort background lookup of the detected group's display name;
    never raises, never blocks the webhook response."""
    try:
        summary = await state.line_client.get_group_summary(group_id)
    except Exception:
        summary = None
    if summary and state.group_detect.group_id == group_id:
        state.group_detect.group_name = summary.get("groupName")


def build_admin_app(state: AppState) -> FastAPI:
    app = FastAPI(title="linebot-lab (admin)")
    _attach_state(app, state)
    app.add_middleware(
        SessionMiddleware,
        secret_key=load_or_create_session_secret(state.data_dir),
        https_only=(state.settings.setup_ui_exposure == "public"),
        same_site="lax",
    )
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(admin_router)
    return app


async def run() -> None:
    settings = get_settings()
    _configure_logging(settings)

    state = AppState.create(settings)
    # Nothing is required to boot -- see app.config.Settings. Missing
    # configuration is reported, loudly, one WARNING per still-unconfigured
    # capability, rather than aborting startup the way
    # require_backend_credentials() used to (see that method's docstring).
    missing = state.settings.missing_requirements(onedrive_authorized=state.onedrive.is_authorized())
    _GROUP_LABEL = {"line": "LINE", "onedrive": "OneDrive", "ocr": f"OCR ({state.settings.ocr_backend})"}
    _GROUP_SETUP_PAGE = {"line": "Setup > LINE", "onedrive": "Setup > OneDrive", "ocr": "Setup > OCR"}
    if missing:
        for group, items in missing.items():
            logger.warning(
                "%s is not fully configured yet: %s -- finish this in the admin UI (%s), or via .env.",
                _GROUP_LABEL.get(group, group),
                "; ".join(items),
                _GROUP_SETUP_PAGE.get(group, "the admin UI"),
            )
    else:
        logger.info("All required configuration present (LINE, OneDrive, OCR).")

    plaintext_password = ensure_admin_password(state.config_store)
    if plaintext_password:
        logger.warning(
            "=" * 70 + "\n"
            "FIRST BOOT: generated an admin UI password (find this again with "
            "`docker compose logs` -- it will not be shown again):\n\n"
            f"    {plaintext_password}\n\n"
            "Log in at the admin UI (port 8001) and change it under Setup > General.\n"
            + "=" * 70
        )

    logger.info(
        "linebot-lab starting: ocr_backend=%s onedrive_root=%s setup_ui_exposure=%s",
        state.settings.ocr_backend,
        state.settings.onedrive_root,
        state.settings.setup_ui_exposure,
    )

    state.queue = asyncio.Queue()
    worker_task = asyncio.create_task(_worker(state))

    public_app = build_public_app(state)
    admin_app = build_admin_app(state)

    servers = [
        uvicorn.Server(uvicorn.Config(public_app, host="0.0.0.0", port=8000, log_level=state.settings.log_level.lower())),
        uvicorn.Server(uvicorn.Config(admin_app, host="0.0.0.0", port=8001, log_level=state.settings.log_level.lower())),
    ]

    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await state.aclose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
