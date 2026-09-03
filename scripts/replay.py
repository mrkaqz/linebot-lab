#!/usr/bin/env python3
"""Replay a saved LINE webhook payload (JSON shaped like the body LINE
posts to /line/webhook) through the pipeline, without a live webhook.
Useful for reproducing/debugging one specific event.

The LINE image content is still fetched from the real LINE API (LINE only
retains message content for a limited window, so this only works for
recent messages) -- --dry-run only skips the OneDrive upload (mocked).

Usage:
    python scripts/replay.py payload.json
    python scripts/replay.py payload.json --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.extract import build_markitdown
from app.line_client import LineClient
from app.onedrive import OneDriveClient
from app.pipeline import process_image_event
from app.store import Store


async def main_async(payload_path: Path, dry_run: bool) -> None:
    settings = get_settings()
    settings.require_backend_credentials()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "events" in payload:
        events = payload["events"]
    elif isinstance(payload, list):
        events = payload
    else:
        events = [payload]

    store = Store(settings.data_dir / "linebot_lab_replay.sqlite3")
    line_client = LineClient(settings.line_channel_access_token)
    markitdown = build_markitdown(settings)

    onedrive: object
    if dry_run:
        onedrive = AsyncMock()
        onedrive.upload_pair.return_value = ("(dry-run)/photo.jpg", "(dry-run)/photo.md")
    else:
        onedrive = OneDriveClient(settings.ms_client_id, settings.ms_redirect_uri, settings.data_dir)

    try:
        for event in events:
            if event.get("type") != "message" or event.get("message", {}).get("type") != "image":
                print(f"Skipping non-image event: type={event.get('type')}")
                continue
            print(f"Replaying message {event['message']['id']}...")
            await process_image_event(
                event,
                settings=settings,
                line_client=line_client,
                markitdown=markitdown,
                onedrive=onedrive,
                store=store,
            )
    finally:
        await line_client.aclose()
        if not dry_run:
            await onedrive.aclose()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("payload", type=Path, help="Path to a saved webhook JSON payload")
    parser.add_argument("--dry-run", action="store_true", help="Skip the OneDrive upload (mocked)")
    args = parser.parse_args()
    asyncio.run(main_async(args.payload, args.dry_run))


if __name__ == "__main__":
    main()
