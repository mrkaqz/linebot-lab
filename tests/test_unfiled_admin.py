"""Admin router: the unfiled-queue resolve/dismiss endpoints. A successful
move marks the row resolved; a failed move leaves it unresolved and surfaces
the error rather than marking it resolved optimistically.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.auth import set_admin_password
from app.onedrive import OneDriveError


def _login(client, state, password="test-password-123"):
    set_admin_password(state.config_store, password)
    resp = client.post("/login", data={"password": password}, follow_redirects=False)
    assert resp.status_code == 303


def _seed_unfiled_row(state) -> int:
    state.store.record_unfiled(
        message_id="msg-1",
        received_at=1756900000.0,
        jpg_path="/LabResults/_UNFILED/2026-09-03/2026-09-03.jpg",
        md_path="/LabResults/_UNFILED/2026-09-03/2026-09-03.md",
        reason="no OPD number found",
    )
    rows = state.store.list_unfiled(resolved=False)
    return rows[0]["id"]


def test_resolve_success_moves_and_marks_resolved(admin_client_factory):
    client, state = admin_client_factory()
    _login(client, state)
    row_id = _seed_unfiled_row(state)

    state.onedrive = AsyncMock()
    state.onedrive.move_pair.return_value = ("/LabResults/12345/2026-09-03.jpg", "/LabResults/12345/2026-09-03.md")

    response = client.post(f"/unfiled/{row_id}/resolve", data={"opd_number": "12345"}, follow_redirects=False)
    assert response.status_code == 303

    state.onedrive.move_pair.assert_awaited_once()
    call = state.onedrive.move_pair.await_args
    assert call.args[0] == "/LabResults/_UNFILED/2026-09-03/2026-09-03.jpg"
    assert call.args[1] == "/LabResults/_UNFILED/2026-09-03/2026-09-03.md"
    assert call.args[2] == "/LabResults/12345"

    row = state.store.get_unfiled(row_id)
    assert row["resolved"] == 1
    assert row["resolution"] == "filed"
    assert row["jpg_path"] == "/LabResults/12345/2026-09-03.jpg"

    activity = state.store.recent_activity(limit=5)
    assert any(a["kind"] == "resolved" and a["opd_number"] == "12345" for a in activity)


def test_resolve_failure_leaves_row_unresolved(admin_client_factory):
    client, state = admin_client_factory()
    _login(client, state)
    row_id = _seed_unfiled_row(state)

    state.onedrive = AsyncMock()
    state.onedrive.move_pair.side_effect = OneDriveError("graph is down")

    response = client.post(f"/unfiled/{row_id}/resolve", data={"opd_number": "12345"}, follow_redirects=False)
    assert response.status_code == 303  # redirects back to /unfiled with a flash error

    row = state.store.get_unfiled(row_id)
    assert row["resolved"] == 0
    assert row["resolution"] is None

    activity = state.store.recent_activity(limit=5)
    assert not any(a["kind"] == "resolved" for a in activity)


def test_dismiss_marks_resolved_without_moving(admin_client_factory):
    client, state = admin_client_factory()
    _login(client, state)
    row_id = _seed_unfiled_row(state)

    state.onedrive = AsyncMock()

    response = client.post(f"/unfiled/{row_id}/dismiss", follow_redirects=False)
    assert response.status_code == 303

    state.onedrive.move_pair.assert_not_awaited()

    row = state.store.get_unfiled(row_id)
    assert row["resolved"] == 1
    assert row["resolution"] == "dismissed"


def test_resolve_with_picked_folder_id_resolves_live_path_not_double_nested(admin_client_factory):
    """When a OneDrive folder has been picked (onedrive_folder_id set), the
    destination must be built from the folder's freshly-resolved absolute
    path, and move_pair must NOT also be given root_item_id -- passing both
    would double-nest the destination under the picked folder's own
    subtree (a bug this test guards against).
    """
    client, state = admin_client_factory(onedrive_folder_id="picked-folder-item-id", onedrive_folder_path="/LabResults/Custom")
    _login(client, state)
    row_id = _seed_unfiled_row(state)

    state.onedrive = AsyncMock()
    state.onedrive.resolve_item_path.return_value = "/LabResults/Custom"
    state.onedrive.move_pair.return_value = ("/LabResults/Custom/99999/2026-09-03.jpg", "/LabResults/Custom/99999/2026-09-03.md")

    response = client.post(f"/unfiled/{row_id}/resolve", data={"opd_number": "99999"}, follow_redirects=False)
    assert response.status_code == 303

    state.onedrive.resolve_item_path.assert_awaited_once_with("picked-folder-item-id")
    call = state.onedrive.move_pair.await_args
    assert call.args[2] == "/LabResults/Custom/99999"
    assert call.kwargs.get("root_item_id") is None  # absolute path -- must not also anchor on an item id


def test_resolve_already_resolved_row_is_a_noop(admin_client_factory):
    client, state = admin_client_factory()
    _login(client, state)
    row_id = _seed_unfiled_row(state)
    state.store.resolve_unfiled(row_id, "dismissed")

    state.onedrive = AsyncMock()
    response = client.post(f"/unfiled/{row_id}/resolve", data={"opd_number": "99999"}, follow_redirects=False)
    assert response.status_code == 303
    state.onedrive.move_pair.assert_not_awaited()
