"""OneDriveClient.move_pair: the Graph PATCH move is issued with the right
parent item id and filename, sequence-suffix bumping on a 409, and a
failure raises rather than returning a "success" that the caller could
mistakenly mark resolved.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.onedrive import OneDriveClient, OneDriveError


class _FakeResponse:
    def __init__(self, status_code: int, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


@pytest.fixture
def client(tmp_path):
    c = OneDriveClient("client-id", "https://example.com/oauth/callback", tmp_path)
    c._acquire_token = lambda: "fake-token"  # bypass real MSAL/token acquisition
    return c


@pytest.mark.asyncio
async def test_move_pair_issues_patch_with_correct_parent_and_name(client):
    http = AsyncMock()
    client._http = http

    # get_item_by_path for jpg, then md
    http.get.side_effect = [
        _FakeResponse(200, {"id": "jpg-item-id", "name": "2026-09-03.jpg"}),
        _FakeResponse(200, {"id": "md-item-id", "name": "2026-09-03.md"}),
    ]
    # ensure_folder_path("/LabResults/12345") -- one segment already exists (409) then... actually
    # two segments: "LabResults" and "12345". Both created fresh (201).
    http.post.side_effect = [
        _FakeResponse(201, {"id": "labresults-folder-id"}),
        _FakeResponse(201, {"id": "opd-folder-id"}),
    ]
    # the two moves (jpg, then md), both succeed first try.
    http.patch.side_effect = [
        _FakeResponse(200, {"id": "jpg-item-id"}),
        _FakeResponse(200, {"id": "md-item-id"}),
    ]

    new_jpg, new_md = await client.move_pair(
        "/LabResults/_UNFILED/2026-09-03/2026-09-03.jpg",
        "/LabResults/_UNFILED/2026-09-03/2026-09-03.md",
        "/LabResults/12345",
        "2026-09-03",
    )

    assert new_jpg == "/LabResults/12345/2026-09-03.jpg"
    assert new_md == "/LabResults/12345/2026-09-03.md"

    jpg_call = http.patch.call_args_list[0]
    assert jpg_call.args[0] == "https://graph.microsoft.com/v1.0/me/drive/items/jpg-item-id"
    assert jpg_call.kwargs["json"] == {"parentReference": {"id": "opd-folder-id"}, "name": "2026-09-03.jpg"}

    md_call = http.patch.call_args_list[1]
    assert md_call.args[0] == "https://graph.microsoft.com/v1.0/me/drive/items/md-item-id"
    assert md_call.kwargs["json"] == {"parentReference": {"id": "opd-folder-id"}, "name": "2026-09-03.md"}


@pytest.mark.asyncio
async def test_move_pair_bumps_sequence_on_409(client):
    http = AsyncMock()
    client._http = http

    http.get.side_effect = [
        _FakeResponse(200, {"id": "jpg-item-id", "name": "2026-09-03.jpg"}),
        _FakeResponse(200, {"id": "md-item-id", "name": "2026-09-03.md"}),
    ]
    http.post.side_effect = [_FakeResponse(201, {"id": "opd-folder-id"})]
    # First jpg move attempt (seq=1) 409s -- name taken -- then seq=2 succeeds; md then succeeds.
    http.patch.side_effect = [
        _FakeResponse(409),
        _FakeResponse(200, {"id": "jpg-item-id"}),
        _FakeResponse(200, {"id": "md-item-id"}),
    ]

    new_jpg, new_md = await client.move_pair(
        "/LabResults/_UNFILED/2026-09-03/2026-09-03.jpg",
        "/LabResults/_UNFILED/2026-09-03/2026-09-03.md",
        "12345",
        "2026-09-03",
    )

    assert new_jpg == "12345/2026-09-03_2.jpg"
    assert new_md == "12345/2026-09-03_2.md"


@pytest.mark.asyncio
async def test_move_pair_raises_on_hard_failure_not_409(client):
    http = AsyncMock()
    client._http = http

    http.get.side_effect = [
        _FakeResponse(200, {"id": "jpg-item-id", "name": "2026-09-03.jpg"}),
        _FakeResponse(200, {"id": "md-item-id", "name": "2026-09-03.md"}),
    ]
    http.post.side_effect = [_FakeResponse(201, {"id": "opd-folder-id"})]
    http.patch.side_effect = [_FakeResponse(500, text="server error")]

    with pytest.raises(OneDriveError):
        await client.move_pair(
            "/LabResults/_UNFILED/2026-09-03/2026-09-03.jpg",
            "/LabResults/_UNFILED/2026-09-03/2026-09-03.md",
            "12345",
            "2026-09-03",
        )
