"""The admin UI: setup/config pages, the status dashboard, and the unfiled
queue. One APIRouter, included on the admin app (port 8001) always, and
ALSO on the public app (port 8000) when SETUP_UI_EXPOSURE=public -- see
app/main.py. Every route here except /login and /static is guarded by
`require_login_page`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..auth import (
    check_admin_password,
    client_key,
    is_logged_in,
    log_in,
    log_out,
    require_login,
    require_login_page,
    set_admin_password,
)
from ..ocr import build_backend
from ..onedrive import OneDriveError
from ..runtime import AppState

logger = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

SAMPLE_IMAGE_PATH = Path(__file__).resolve().parent.parent / "static" / "sample_lab_result.jpg"

OCR_KEY_FIELD = {"claude": "anthropic_api_key", "gemini": "gemini_api_key", "tesseract": None}


def rt(request: Request) -> AppState:
    return request.app.state.rt


def _flash(request: Request, message: str, category: str = "info") -> None:
    flashes = request.session.setdefault("_flashes", [])
    flashes.append({"message": message, "category": category})


def _pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("_flashes", [])


def _render(request: Request, template: str, **context) -> HTMLResponse:
    context.setdefault("flashes", _pop_flashes(request))
    context.setdefault("logged_in", is_logged_in(request))
    return templates.TemplateResponse(request, template, context)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _masked(state: AppState, field: str) -> dict:
    return {"is_set": state.config_store.is_set(field)}


# ---------------------------------------------------------------- login ----


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_logged_in(request):
        return _redirect("/")
    return _render(request, "login.html")


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    state = rt(request)
    key = client_key(request)

    locked_seconds = state.login_rate_limiter.is_locked(key)
    if locked_seconds is not None:
        _flash(request, f"Too many failed attempts. Try again in {int(locked_seconds // 60) + 1} minute(s).", "error")
        return _redirect("/login")

    if check_admin_password(state.config_store, password):
        state.login_rate_limiter.record_success(key)
        log_in(request)
        return _redirect("/")

    state.login_rate_limiter.record_failure(key)
    logger.warning("Failed admin login attempt from %s", key)
    _flash(request, "Incorrect password.", "error")
    return _redirect("/login")


@router.post("/logout")
async def logout(request: Request):
    log_out(request)
    return _redirect("/login")


# ------------------------------------------------------------ dashboard ----


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login_page)])
async def dashboard(request: Request):
    state = rt(request)
    settings = state.settings

    missing = []
    if not settings.line_channel_secret or not settings.line_channel_access_token:
        missing.append(("LINE channel credentials", "/setup/line"))
    if not settings.line_lab_group_id:
        missing.append(("LINE lab group id", "/setup/line"))
    if settings.ocr_backend == "claude" and not settings.anthropic_api_key:
        missing.append(("Anthropic API key (OCR backend = claude)", "/setup/ocr"))
    if settings.ocr_backend == "gemini" and not settings.gemini_api_key:
        missing.append(("Gemini API key (OCR backend = gemini)", "/setup/ocr"))
    if not state.onedrive.is_authorized():
        missing.append(("OneDrive sign-in", "/setup/onedrive"))

    if missing:
        return _render(request, "dashboard.html", configured=False, missing=missing)

    onedrive_connected = state.onedrive.is_authorized()
    account = state.onedrive.get_account_info()
    last_filed_row = state.store.last_filed()
    unfiled_count = state.store.count_unfiled_unresolved()
    queue_depth = state.queue.qsize() if state.queue is not None else 0

    try:
        tz = ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    midnight_today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    filed_today = state.store.count_filed_since(midnight_today.timestamp())

    recent = state.store.recent_activity(limit=20)

    return _render(
        request,
        "dashboard.html",
        configured=True,
        onedrive_connected=onedrive_connected,
        onedrive_account=account,
        last_filed=dict(last_filed_row) if last_filed_row else None,
        unfiled_count=unfiled_count,
        queue_depth=queue_depth,
        filed_today=filed_today,
        recent_activity=[dict(row) for row in recent],
        onedrive_root_display=state.effective_onedrive_root_path(),
    )


# --------------------------------------------------------------- setup -----


@router.get("/setup", dependencies=[Depends(require_login_page)])
async def setup_index():
    return _redirect("/setup/line")


@router.get("/setup/line", response_class=HTMLResponse, dependencies=[Depends(require_login_page)])
async def setup_line_page(request: Request):
    state = rt(request)
    return _render(
        request,
        "setup_line.html",
        settings=state.settings,
        secret_state=_masked(state, "line_channel_secret"),
        token_state=_masked(state, "line_channel_access_token"),
        detect=state.group_detect,
    )


@router.post("/setup/line/save", dependencies=[Depends(require_login_page)])
async def setup_line_save(
    request: Request,
    channel_secret: str = Form(""),
    clear_channel_secret: Optional[str] = Form(None),
    channel_access_token: str = Form(""),
    clear_channel_access_token: Optional[str] = Form(None),
    group_id: str = Form(""),
    admin_line_id: str = Form(""),
):
    state = rt(request)
    changed: set[str] = set()

    if clear_channel_secret:
        state.config_store.clear("line_channel_secret")
        changed.add("line_channel_secret")
    elif channel_secret.strip():
        state.config_store.set("line_channel_secret", channel_secret.strip())
        changed.add("line_channel_secret")

    if clear_channel_access_token:
        state.config_store.clear("line_channel_access_token")
        changed.add("line_channel_access_token")
    elif channel_access_token.strip():
        state.config_store.set("line_channel_access_token", channel_access_token.strip())
        changed.add("line_channel_access_token")

    group_id = group_id.strip()
    if group_id:
        state.config_store.set("line_lab_group_id", group_id)
    else:
        state.config_store.clear("line_lab_group_id")
    changed.add("line_lab_group_id")

    admin_line_id = admin_line_id.strip()
    if admin_line_id:
        state.config_store.set("admin_line_id", admin_line_id)
    else:
        state.config_store.clear("admin_line_id")
    changed.add("admin_line_id")

    notes = state.apply_changes(changed)
    _flash(request, "LINE settings saved." + (" " + " ".join(notes) if notes else ""), "success")
    return _redirect("/setup/line")


@router.post("/setup/line/detect/start", dependencies=[Depends(require_login)])
async def setup_line_detect_start(request: Request):
    state = rt(request)
    state.group_detect.start()
    return JSONResponse({"listening": True, "listening_until": state.group_detect.listening_until})


@router.get("/setup/line/detect/status", dependencies=[Depends(require_login)])
async def setup_line_detect_status(request: Request):
    state = rt(request)
    d = state.group_detect
    return JSONResponse(
        {
            "listening": d.is_listening(),
            "group_id": d.group_id,
            "group_name": d.group_name,
            "found_at": d.found_at,
        }
    )


# ----------------------------------------------------------- ocr setup -----


@router.get("/setup/ocr", response_class=HTMLResponse, dependencies=[Depends(require_login_page)])
async def setup_ocr_page(request: Request):
    state = rt(request)
    return _render(
        request,
        "setup_ocr.html",
        settings=state.settings,
        anthropic_state=_masked(state, "anthropic_api_key"),
        gemini_state=_masked(state, "gemini_api_key"),
    )


@router.post("/setup/ocr/save", dependencies=[Depends(require_login_page)])
async def setup_ocr_save(
    request: Request,
    backend: str = Form(...),
    claude_model: str = Form(""),
    anthropic_api_key: str = Form(""),
    clear_anthropic_api_key: Optional[str] = Form(None),
    gemini_model: str = Form(""),
    gemini_api_key: str = Form(""),
    clear_gemini_api_key: Optional[str] = Form(None),
):
    state = rt(request)
    if backend not in ("claude", "gemini", "tesseract"):
        _flash(request, f"Unknown OCR backend: {backend}", "error")
        return _redirect("/setup/ocr")

    changed: set[str] = {"ocr_backend"}
    state.config_store.set("ocr_backend", backend)

    if claude_model.strip():
        state.config_store.set("claude_model", claude_model.strip())
        changed.add("claude_model")
    if gemini_model.strip():
        state.config_store.set("gemini_model", gemini_model.strip())
        changed.add("gemini_model")

    if clear_anthropic_api_key:
        state.config_store.clear("anthropic_api_key")
        changed.add("anthropic_api_key")
    elif anthropic_api_key.strip():
        state.config_store.set("anthropic_api_key", anthropic_api_key.strip())
        changed.add("anthropic_api_key")

    if clear_gemini_api_key:
        state.config_store.clear("gemini_api_key")
        changed.add("gemini_api_key")
    elif gemini_api_key.strip():
        state.config_store.set("gemini_api_key", gemini_api_key.strip())
        changed.add("gemini_api_key")

    try:
        notes = state.apply_changes(changed)
    except Exception as exc:  # e.g. require_backend_credentials-style misconfig surfaced by a rebuild
        logger.exception("Failed to hot-reload OCR backend after save")
        _flash(request, f"Saved, but the new OCR backend could not be loaded: {exc}", "error")
        return _redirect("/setup/ocr")

    _flash(request, "OCR settings saved." + (" " + " ".join(notes) if notes else ""), "success")
    return _redirect("/setup/ocr")


@router.post("/setup/ocr/test", dependencies=[Depends(require_login)])
async def setup_ocr_test(
    request: Request,
    backend: str = Form(...),
    api_key: str = Form(""),
):
    state = rt(request)
    if backend not in ("claude", "gemini", "tesseract"):
        return JSONResponse({"ok": False, "error": f"Unknown backend: {backend}"}, status_code=400)

    key_field = OCR_KEY_FIELD[backend]
    overrides = {"ocr_backend": backend}
    if key_field:
        overrides[key_field] = api_key.strip() or getattr(state.settings, key_field, None)
    temp_settings = state.settings.model_copy(update=overrides)

    try:
        temp_settings.require_backend_credentials()
        converter = build_backend(temp_settings)
        image_bytes = SAMPLE_IMAGE_PATH.read_bytes()
        result = await asyncio.to_thread(converter._extract, image_bytes, "image/jpeg")
    except Exception as exc:
        logger.warning("OCR backend test failed for backend=%s: %s", backend, exc)
        return JSONResponse({"ok": False, "error": str(exc)})

    return JSONResponse(
        {
            "ok": True,
            "opd_number": result.opd_number,
            "text": (result.markdown or "")[:2000],
        }
    )


# ------------------------------------------------------- onedrive setup ----


@router.get("/setup/onedrive", response_class=HTMLResponse, dependencies=[Depends(require_login_page)])
async def setup_onedrive_page(request: Request):
    state = rt(request)
    connected = state.onedrive.is_authorized()
    account = state.onedrive.get_account_info()
    return _render(
        request,
        "setup_onedrive.html",
        connected=connected,
        account=account,
        onedrive_root=state.settings.onedrive_root,
        folder_id=state.settings.onedrive_folder_id,
        folder_path=state.settings.onedrive_folder_path,
        oauth_start_url=f"/oauth/start?secret={state.settings.oauth_setup_secret}",
    )


@router.get("/setup/onedrive/browse", dependencies=[Depends(require_login)])
async def setup_onedrive_browse(request: Request, item_id: Optional[str] = Query(None)):
    state = rt(request)
    try:
        folders = await state.onedrive.list_children(item_id or None)
    except OneDriveError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "folders": [{"id": f["id"], "name": f["name"]} for f in folders]})


@router.post("/setup/onedrive/new-folder", dependencies=[Depends(require_login)])
async def setup_onedrive_new_folder(request: Request, parent_item_id: str = Form(""), name: str = Form(...)):
    state = rt(request)
    name = name.strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Folder name cannot be blank."}, status_code=400)
    try:
        item = await state.onedrive.create_folder(parent_item_id or None, name)
    except OneDriveError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "id": item["id"], "name": item["name"]})


@router.post("/setup/onedrive/select", dependencies=[Depends(require_login_page)])
async def setup_onedrive_select(request: Request, item_id: str = Form(...), path: str = Form(...)):
    state = rt(request)
    state.config_store.set("onedrive_folder_id", item_id)
    state.config_store.set("onedrive_folder_path", path)
    state.apply_changes({"onedrive_folder_id", "onedrive_folder_path"})
    _flash(request, f"Filing folder set to {path}.", "success")
    return _redirect("/setup/onedrive")


@router.post("/setup/onedrive/disconnect", dependencies=[Depends(require_login_page)])
async def setup_onedrive_disconnect(request: Request):
    state = rt(request)
    state.onedrive.disconnect()
    _flash(request, "Disconnected from OneDrive.", "success")
    return _redirect("/setup/onedrive")


# -------------------------------------------------------- general setup ----


@router.get("/setup/general", response_class=HTMLResponse, dependencies=[Depends(require_login_page)])
async def setup_general_page(request: Request):
    state = rt(request)
    return _render(request, "setup_general.html", settings=state.settings)


@router.post("/setup/general/save", dependencies=[Depends(require_login_page)])
async def setup_general_save(
    request: Request,
    timezone: str = Form(...),
    opd_regex: str = Form(...),
    setup_ui_exposure: str = Form(...),
):
    state = rt(request)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        _flash(request, f"Unknown timezone: {timezone}", "error")
        return _redirect("/setup/general")
    try:
        re.compile(opd_regex)
    except re.error as exc:
        _flash(request, f"Invalid OPD regex: {exc}", "error")
        return _redirect("/setup/general")
    if setup_ui_exposure not in ("lan", "public"):
        _flash(request, f"Unknown SETUP_UI_EXPOSURE: {setup_ui_exposure}", "error")
        return _redirect("/setup/general")

    changed = set()
    state.config_store.set("timezone", timezone)
    changed.add("timezone")
    state.config_store.set("opd_regex", opd_regex)
    changed.add("opd_regex")
    if setup_ui_exposure != state.settings.setup_ui_exposure:
        state.config_store.set("setup_ui_exposure", setup_ui_exposure)
        changed.add("setup_ui_exposure")

    notes = state.apply_changes(changed)
    _flash(request, "General settings saved." + (" " + " ".join(notes) if notes else ""), "success")
    return _redirect("/setup/general")


@router.post("/setup/general/test-regex", dependencies=[Depends(require_login)])
async def setup_general_test_regex(request: Request, pattern: str = Form(...), sample_text: str = Form("")):
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return JSONResponse({"ok": False, "error": str(exc)})

    matches = []
    for m in compiled.finditer(sample_text):
        matches.append({"match": m.group(0), "group1": m.group(1) if m.groups() else None})
    return JSONResponse({"ok": True, "matches": matches})


@router.post("/setup/general/password", dependencies=[Depends(require_login_page)])
async def setup_general_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    state = rt(request)
    if not check_admin_password(state.config_store, current_password):
        _flash(request, "Current password is incorrect.", "error")
        return _redirect("/setup/general")
    if len(new_password) < 8:
        _flash(request, "New password must be at least 8 characters.", "error")
        return _redirect("/setup/general")
    if new_password != confirm_password:
        _flash(request, "New password and confirmation do not match.", "error")
        return _redirect("/setup/general")

    set_admin_password(state.config_store, new_password)
    _flash(request, "Admin password changed.", "success")
    return _redirect("/setup/general")


# ---------------------------------------------------------- unfiled queue --


@router.get("/unfiled", response_class=HTMLResponse, dependencies=[Depends(require_login_page)])
async def unfiled_list(request: Request):
    state = rt(request)
    rows = [dict(row) for row in state.store.list_unfiled(resolved=False)]
    for row in rows:
        row["md_text"] = None
        if row.get("md_path"):
            try:
                content = await state.onedrive.download_bytes(path=row["md_path"])
                row["md_text"] = content.decode("utf-8", errors="replace")
            except OneDriveError as exc:
                logger.warning("Failed to fetch transcript for unfiled row %s: %s", row["id"], exc)
                row["md_text"] = f"(could not load transcript: {exc})"
    return _render(request, "unfiled_list.html", rows=rows)


@router.get("/unfiled/{row_id}/photo", dependencies=[Depends(require_login)])
async def unfiled_photo(request: Request, row_id: int):
    state = rt(request)
    row = state.store.get_unfiled(row_id)
    if row is None or not row["jpg_path"]:
        return Response(status_code=404)
    try:
        content = await state.onedrive.download_bytes(path=row["jpg_path"])
    except OneDriveError as exc:
        logger.warning("Failed to proxy unfiled photo %s: %s", row_id, exc)
        return Response(status_code=502)
    return Response(content=content, media_type="image/jpeg")


@router.post("/unfiled/{row_id}/resolve", dependencies=[Depends(require_login_page)])
async def unfiled_resolve(request: Request, row_id: int, opd_number: str = Form(...)):
    state = rt(request)
    opd_number = opd_number.strip()
    row = state.store.get_unfiled(row_id)

    if row is None:
        _flash(request, "Unfiled entry not found.", "error")
        return _redirect("/unfiled")
    if row["resolved"]:
        _flash(request, "This entry was already resolved.", "error")
        return _redirect("/unfiled")
    if not opd_number:
        _flash(request, "Enter an OPD number.", "error")
        return _redirect("/unfiled")
    if not row["jpg_path"] or not row["md_path"]:
        _flash(request, "This entry has no filed photo/transcript to move.", "error")
        return _redirect("/unfiled")

    base_stem = Path(row["jpg_path"]).stem

    try:
        # onedrive_folder_path/onedrive_root are already absolute
        # drive-root-relative paths (resolve_item_path returns one too), so
        # move_pair is called WITHOUT root_item_id here -- passing both an
        # absolute path and a root_item_id would double-nest the
        # destination under the picked folder's own subtree. Re-resolve
        # from the live item id (rather than trusting the cached
        # onedrive_folder_path) so a rename since the folder was picked
        # doesn't misfile this move.
        if state.settings.onedrive_folder_id:
            root_path = await state.onedrive.resolve_item_path(state.settings.onedrive_folder_id)
        else:
            root_path = state.settings.onedrive_root
        dest_folder = f"{root_path.rstrip('/')}/{opd_number}"

        new_jpg_path, new_md_path = await state.onedrive.move_pair(
            row["jpg_path"],
            row["md_path"],
            dest_folder,
            base_stem,
        )
    except OneDriveError as exc:
        logger.warning("Failed to move unfiled row %s to OPD %s: %s", row_id, opd_number, exc)
        _flash(request, f"Move failed, entry left unresolved: {exc}", "error")
        return _redirect("/unfiled")

    state.store.resolve_unfiled(row_id, "filed", new_jpg_path=new_jpg_path, new_md_path=new_md_path)
    state.store.record_activity("resolved", opd_number, detail=new_jpg_path)
    _flash(request, f"Filed under OPD {opd_number}.", "success")
    return _redirect("/unfiled")


@router.post("/unfiled/{row_id}/dismiss", dependencies=[Depends(require_login_page)])
async def unfiled_dismiss(request: Request, row_id: int):
    state = rt(request)
    row = state.store.get_unfiled(row_id)
    if row is None:
        _flash(request, "Unfiled entry not found.", "error")
        return _redirect("/unfiled")
    if row["resolved"]:
        _flash(request, "This entry was already resolved.", "error")
        return _redirect("/unfiled")

    state.store.resolve_unfiled(row_id, "dismissed")
    state.store.record_activity("dismissed", None, detail=row["jpg_path"] or "")
    _flash(request, "Dismissed.", "success")
    return _redirect("/unfiled")
