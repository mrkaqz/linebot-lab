"""Per-image job: download from LINE, extract via MarkItDown + the
configured OCR backend, compute the OneDrive destination, and upload.

`received_date()` and `resolve_destination()` are pure and unit-tested
directly; `process_image_event()` is the async orchestration that ties LINE
download, extraction, and OneDrive upload together for one event.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from markitdown import MarkItDown

from .config import Settings
from .extract import extract
from .line_client import LineClient
from .onedrive import OneDriveClient
from .store import Store

logger = logging.getLogger(__name__)

UNFILED_FOLDER = "_UNFILED"


@dataclass
class Destination:
    folder_path: str
    stem: str


def received_date(timestamp_ms: int, timezone: str) -> str:
    """Convert a LINE event `timestamp` (epoch milliseconds, UTC) to a
    YYYY-MM-DD date string in `timezone`.

    This is deliberately derived from the event timestamp, NOT server local
    time and NOT datetime.now() -- a photo that arrives at 23:30 UTC must
    file under the *next* Bangkok date (UTC+7).
    """
    dt_utc = datetime.fromtimestamp(timestamp_ms / 1000, tz=ZoneInfo("UTC"))
    dt_local = dt_utc.astimezone(ZoneInfo(timezone))
    return dt_local.date().isoformat()


def resolve_destination(onedrive_root: str, opd_number: Optional[str], date_str: str) -> Destination:
    """Compute the OneDrive folder path and filename stem for a result.

    OPD found:     {root}/{opd}/{date}.jpg + .md
    OPD not found: {root}/_UNFILED/{date}/{date}.jpg + .md
    """
    root = onedrive_root.rstrip("/")
    if opd_number:
        return Destination(folder_path=f"{root}/{opd_number}", stem=date_str)
    return Destination(folder_path=f"{root}/{UNFILED_FOLDER}/{date_str}", stem=date_str)


async def process_image_event(
    event: dict,
    *,
    settings: Settings,
    line_client: LineClient,
    markitdown: MarkItDown,
    onedrive: OneDriveClient,
    store: Store,
) -> None:
    """Handle one LINE image-message event end to end.

    Assumes the caller has already established that this event should be
    processed (group filter, message-type filter, and the idempotency
    check against the `processed` table).
    """
    message_id = event["message"]["id"]
    timestamp_ms = event["timestamp"]
    date_str = received_date(timestamp_ms, settings.timezone)

    with tempfile.TemporaryDirectory(prefix="linebot-lab-") as tmpdir:
        image_path = Path(tmpdir) / f"{message_id}.jpg"
        # Download first -- LINE only retains message content for a limited
        # window, and extraction/upload can be slow.
        await line_client.download_content(message_id, image_path)

        result = None
        try:
            result = extract(markitdown, str(image_path), settings)
        except Exception:
            logger.exception("Extraction failed for message %s; filing to _UNFILED", message_id)

        if result is not None:
            logger.info(
                "Extracted message %s: opd=%s confidence=%s",
                message_id,
                result.opd_number,
                result.confidence,
            )

        opd_number = result.opd_number if result else None
        markdown_text = result.markdown if result else "(extraction failed -- see server logs)"

        destination = resolve_destination(settings.onedrive_root, opd_number, date_str)
        jpg_bytes = image_path.read_bytes()

        jpg_path, md_path = await onedrive.upload_pair(
            destination.folder_path,
            destination.stem,
            jpg_bytes,
            markdown_text,
        )

    if opd_number:
        logger.info("Filed message %s under OPD %s at %s", message_id, opd_number, jpg_path)
        return

    reason = "no OPD number found" if result is not None else "extraction failed"
    store.record_unfiled(
        message_id=message_id,
        received_at=timestamp_ms / 1000,
        jpg_path=jpg_path,
        md_path=md_path,
        reason=reason,
    )
    logger.warning("Filed message %s as UNFILED (%s) at %s", message_id, reason, jpg_path)

    if settings.admin_line_id:
        try:
            await line_client.push(
                settings.admin_line_id,
                f"Lab result could not be matched to an OPD number ({reason}) and was filed under {jpg_path}. Please review.",
            )
        except Exception:
            logger.exception("Failed to push admin notification for unfiled message %s", message_id)
