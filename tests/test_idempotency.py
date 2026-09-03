"""Idempotency: the same LINE messageId submitted twice (a webhook retry)
must result in exactly one OneDrive upload.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import pipeline as pipeline_module
from app.pipeline import process_image_event
from app.store import Store


@pytest.mark.asyncio
async def test_duplicate_message_id_uploads_exactly_once(tmp_path, make_settings, monkeypatch):
    settings = make_settings(data_dir=tmp_path)
    store = Store(tmp_path / "idempotency.sqlite3")

    line_client = AsyncMock()

    async def fake_download(message_id, dest_path):
        dest_path.write_bytes(b"fake-jpg-bytes")

    line_client.download_content.side_effect = fake_download

    onedrive = AsyncMock()
    onedrive.upload_pair.return_value = ("/LabResults/12345/2026-09-03.jpg", "/LabResults/12345/2026-09-03.md")

    fake_result = SimpleNamespace(markdown="body", opd_number="12345", patient_name=None, confidence=0.9)
    monkeypatch.setattr(pipeline_module, "extract", lambda md, path, settings: fake_result)

    event = {
        "type": "message",
        "message": {"id": "msg-duplicate-1", "type": "image"},
        "timestamp": 1756900000000,
        "source": {"type": "group", "groupId": "Ctestgroup0000000000000000000000"},
    }

    # Simulate the webhook handler's idempotency gate (INSERT OR IGNORE,
    # skip if rowcount == 0) followed by worker processing, invoked twice --
    # exactly what happens when LINE retries a webhook delivery.
    for _ in range(2):
        if store.mark_processed(event["message"]["id"]):
            await process_image_event(
                event,
                settings=settings,
                line_client=line_client,
                markitdown=object(),  # unused: extract() is monkeypatched above
                onedrive=onedrive,
                store=store,
            )

    assert onedrive.upload_pair.call_count == 1
    assert line_client.download_content.call_count == 1


@pytest.mark.asyncio
async def test_two_distinct_message_ids_both_upload(tmp_path, make_settings, monkeypatch):
    settings = make_settings(data_dir=tmp_path)
    store = Store(tmp_path / "idempotency2.sqlite3")

    line_client = AsyncMock()

    async def fake_download(message_id, dest_path):
        dest_path.write_bytes(b"fake-jpg-bytes")

    line_client.download_content.side_effect = fake_download

    onedrive = AsyncMock()
    onedrive.upload_pair.return_value = ("/LabResults/12345/2026-09-03.jpg", "/LabResults/12345/2026-09-03.md")

    fake_result = SimpleNamespace(markdown="body", opd_number="12345", patient_name=None, confidence=0.9)
    monkeypatch.setattr(pipeline_module, "extract", lambda md, path, settings: fake_result)

    for message_id in ("msg-a", "msg-b"):
        event = {
            "type": "message",
            "message": {"id": message_id, "type": "image"},
            "timestamp": 1756900000000,
            "source": {"type": "group", "groupId": "Ctestgroup0000000000000000000000"},
        }
        if store.mark_processed(message_id):
            await process_image_event(
                event,
                settings=settings,
                line_client=line_client,
                markitdown=object(),
                onedrive=onedrive,
                store=store,
            )

    assert onedrive.upload_pair.call_count == 2
