"""FastAPI application: LINE webhook intake, OneDrive OAuth setup routes,
and a health check.

The webhook handler does the minimum synchronous work needed for
correctness (signature verification, event filtering, the idempotency
check) and then hands each image event to an in-process `asyncio.Queue`,
returning HTTP 200 immediately. A background task started in the app
lifespan drains the queue and does the slow work (download, OCR, upload) --
LINE times out and retries slow webhooks, and doing that work inline would
cause duplicate filings.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from .config import Settings, get_settings
from .extract import build_markitdown
from .line_client import LineClient, verify_signature
from .onedrive import OneDriveAuthError, OneDriveClient
from .pipeline import process_image_event
from .store import Store

logger = logging.getLogger(__name__)


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings)
    settings.require_backend_credentials()  # fail loudly at startup, not on first lab result

    settings.data_dir.mkdir(parents=True, exist_ok=True)

    store = Store(settings.data_dir / "linebot_lab.sqlite3")
    line_client = LineClient(settings.line_channel_access_token)
    markitdown = build_markitdown(settings)
    onedrive = OneDriveClient(settings.ms_client_id, settings.ms_redirect_uri, settings.data_dir)

    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def worker() -> None:
        while True:
            event = await queue.get()
            try:
                await process_image_event(
                    event,
                    settings=settings,
                    line_client=line_client,
                    markitdown=markitdown,
                    onedrive=onedrive,
                    store=store,
                )
            except Exception:
                logger.exception("Unhandled error processing event %s", event.get("message", {}).get("id"))
            finally:
                queue.task_done()

    worker_task = asyncio.create_task(worker())

    app.state.settings = settings
    app.state.store = store
    app.state.line_client = line_client
    app.state.markitdown = markitdown
    app.state.onedrive = onedrive
    app.state.queue = queue

    if not settings.line_lab_group_id:
        logger.warning(
            "LINE_LAB_GROUP_ID is not set -- no messages will be processed. "
            "Watch the logs for 'Message event seen' lines to find the group id, then set it in .env."
        )

    logger.info("linebot-lab started: ocr_backend=%s onedrive_root=%s", settings.ocr_backend, settings.onedrive_root)

    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await line_client.aclose()
        await onedrive.aclose()
        store.close()


app = FastAPI(title="linebot-lab", lifespan=lifespan)


@app.get("/healthz")
async def healthz(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    onedrive: OneDriveClient = request.app.state.onedrive
    queue: asyncio.Queue = request.app.state.queue

    onedrive_ok = onedrive.is_authorized()
    healthy = onedrive_ok and bool(settings.line_lab_group_id)

    return {
        "status": "ok" if healthy else "degraded",
        "onedrive_authorized": onedrive_ok,
        "line_lab_group_id_configured": bool(settings.line_lab_group_id),
        "queue_depth": queue.qsize(),
    }


@app.post("/line/webhook")
async def line_webhook(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    store: Store = request.app.state.store
    queue: asyncio.Queue = request.app.state.queue

    # Read the RAW body before any JSON parsing -- re-serializing the parsed
    # JSON would change the bytes and break the signature check.
    body = await request.body()
    signature = request.headers.get("x-line-signature", "")

    if not verify_signature(settings.line_channel_secret, body, signature):
        logger.warning("Rejected webhook with invalid x-line-signature")
        raise HTTPException(status_code=400, detail="invalid signature")

    payload = await request.json()

    for event in payload.get("events", []):
        source = event.get("source", {})
        group_id = source.get("groupId")

        if event.get("type") == "message":
            # The group id is not visible anywhere in the LINE console --
            # this is how an operator finds it to put in .env.
            logger.info("Message event seen: groupId=%s messageType=%s", group_id, event.get("message", {}).get("type"))

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
        if not store.mark_processed(message_id):
            continue

        await queue.put(event)

    return Response(status_code=200)


@app.get("/oauth/start")
async def oauth_start(request: Request) -> RedirectResponse:
    settings: Settings = request.app.state.settings
    onedrive: OneDriveClient = request.app.state.onedrive

    _require_setup_secret(request, settings)
    auth_url = onedrive.start_auth()
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
async def oauth_callback(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    onedrive: OneDriveClient = request.app.state.onedrive

    _require_setup_secret(request, settings)
    try:
        onedrive.complete_auth(request.query_params)
    except OneDriveAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"status": "ok", "detail": "OneDrive authorization complete."}


def _require_setup_secret(request: Request, settings: Settings) -> None:
    """Guard /oauth/start and /oauth/callback with a shared secret in the
    query string, so a public tunnel URL cannot be hijacked by a stranger
    into authorizing the bot against *their* OneDrive account. See
    README.md for how to register MS_REDIRECT_URI so this secret survives
    Microsoft's redirect back to /oauth/callback.
    """
    secret = request.query_params.get("secret", "")
    if not hmac.compare_digest(secret, settings.oauth_setup_secret):
        raise HTTPException(status_code=403, detail="missing or invalid setup secret")
