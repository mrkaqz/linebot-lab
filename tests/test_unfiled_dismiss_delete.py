"""Dismissing an unfiled entry deletes its files from _UNFILED.

"Dismiss" means the photo was never a lab result -- junk, a duplicate, a
picture of something else. Leaving the pair behind accumulated patient
photos in _UNFILED that nobody would ever look at again, so dismissal now
removes both halves.

Two properties matter and each is tested below:

1. BOTH files go. Deleting the .jpg but leaving the .md (or vice versa)
   leaves an orphan that still contains transcribed patient data.
2. A delete that fails must not wedge the queue. The row is still dismissed,
   but the operator is told plainly which files survived, rather than being
   shown a success message for something that did not happen.

Graph deletes to the recycle bin rather than erasing, which is what makes
this safe enough to do on a single click.
"""

from __future__ import annotations

import pytest

from app.auth import set_admin_password
from app.onedrive import OneDriveError


@pytest.fixture
def unfiled_client(admin_client_factory):
    client, state = admin_client_factory()
    set_admin_password(state.config_store, "test-password-123")
    resp = client.post("/login", data={"password": "test-password-123"}, follow_redirects=False)
    assert resp.status_code == 303, "fixture failed to authenticate"
    return client, state


def _add_row(state, message_id="m1"):
    state.store.record_unfiled(
        message_id=message_id,
        received_at=1757000000.0,
        jpg_path="/LabResults/_UNFILED/2026-09-05/2026-09-05.jpg",
        md_path="/LabResults/_UNFILED/2026-09-05/2026-09-05.md",
        reason="no OPD number found",
    )
    return state.store.list_unfiled()[0]["id"]


class _Recorder:
    """Stands in for OneDriveClient.delete_item."""

    def __init__(self, fail_on=(), missing=()):
        self.deleted: list[str] = []
        self.fail_on = set(fail_on)
        self.missing = set(missing)

    async def __call__(self, *, path: str) -> bool:
        if path in self.fail_on:
            raise OneDriveError(f"boom deleting {path}")
        self.deleted.append(path)
        return path not in self.missing


def test_dismiss_deletes_both_files(unfiled_client, monkeypatch):
    client, state = unfiled_client
    row_id = _add_row(state)
    rec = _Recorder()
    monkeypatch.setattr(state.onedrive, "delete_item", rec)

    client.post(f"/unfiled/{row_id}/dismiss", follow_redirects=False)

    assert rec.deleted == [
        "/LabResults/_UNFILED/2026-09-05/2026-09-05.jpg",
        "/LabResults/_UNFILED/2026-09-05/2026-09-05.md",
    ], "both halves of the pair must be deleted, not just the photo"


def test_dismiss_marks_the_row_resolved(unfiled_client, monkeypatch):
    client, state = unfiled_client
    row_id = _add_row(state)
    monkeypatch.setattr(state.onedrive, "delete_item", _Recorder())

    client.post(f"/unfiled/{row_id}/dismiss", follow_redirects=False)

    assert state.store.get_unfiled(row_id)["resolved"]
    assert state.store.list_unfiled() == []


def test_dismiss_still_clears_the_queue_when_delete_fails(unfiled_client, monkeypatch):
    """A permanently failing delete must not make junk impossible to dismiss."""
    client, state = unfiled_client
    row_id = _add_row(state)
    jpg = "/LabResults/_UNFILED/2026-09-05/2026-09-05.jpg"
    monkeypatch.setattr(state.onedrive, "delete_item", _Recorder(fail_on={jpg}))

    resp = client.post(f"/unfiled/{row_id}/dismiss", follow_redirects=True)

    assert state.store.get_unfiled(row_id)["resolved"]
    body = resp.text
    assert "could not be deleted" in body
    assert jpg in body, "the operator must be told WHICH file is still there"


def test_failed_delete_does_not_claim_success(unfiled_client, monkeypatch):
    client, state = unfiled_client
    row_id = _add_row(state)
    md = "/LabResults/_UNFILED/2026-09-05/2026-09-05.md"
    monkeypatch.setattr(state.onedrive, "delete_item", _Recorder(fail_on={md}))

    body = client.post(f"/unfiled/{row_id}/dismiss", follow_redirects=True).text
    assert "Deleted" not in body or "could not be deleted" in body


def test_already_missing_files_are_not_an_error(unfiled_client, monkeypatch):
    """delete_item returns False for a 404. Dismissing something already
    cleaned up by hand should still succeed quietly."""
    client, state = unfiled_client
    row_id = _add_row(state)
    both = {
        "/LabResults/_UNFILED/2026-09-05/2026-09-05.jpg",
        "/LabResults/_UNFILED/2026-09-05/2026-09-05.md",
    }
    monkeypatch.setattr(state.onedrive, "delete_item", _Recorder(missing=both))

    body = client.post(f"/unfiled/{row_id}/dismiss", follow_redirects=True).text
    assert state.store.get_unfiled(row_id)["resolved"]
    assert "could not be deleted" not in body


def test_dismiss_records_activity(unfiled_client, monkeypatch):
    client, state = unfiled_client
    row_id = _add_row(state)
    monkeypatch.setattr(state.onedrive, "delete_item", _Recorder())

    client.post(f"/unfiled/{row_id}/dismiss", follow_redirects=False)

    kinds = [r["kind"] for r in state.store.recent_activity(limit=5)]
    assert "dismissed" in kinds


def test_resolve_still_moves_rather_than_deletes(unfiled_client, monkeypatch):
    """Filing under a corrected OPD must never delete -- only dismiss does."""
    client, state = unfiled_client
    row_id = _add_row(state)
    rec = _Recorder()
    monkeypatch.setattr(state.onedrive, "delete_item", rec)

    async def fake_move(*args, **kwargs):
        return ("/LabResults/8258/2026-09-05.jpg", "/LabResults/8258/2026-09-05.md")

    monkeypatch.setattr(state.onedrive, "move_pair", fake_move)
    monkeypatch.setattr(state.settings, "onedrive_folder_id", None)

    client.post(f"/unfiled/{row_id}/resolve", data={"opd_number": "8258"}, follow_redirects=False)

    assert rec.deleted == [], "resolving files the result, it must not delete it"


def test_dismissing_twice_is_rejected(unfiled_client, monkeypatch):
    client, state = unfiled_client
    row_id = _add_row(state)
    rec = _Recorder()
    monkeypatch.setattr(state.onedrive, "delete_item", rec)

    client.post(f"/unfiled/{row_id}/dismiss", follow_redirects=False)
    client.post(f"/unfiled/{row_id}/dismiss", follow_redirects=False)

    assert len(rec.deleted) == 2, "the second dismiss must not delete anything again"


def test_photo_is_clickable_to_full_size(unfiled_client):
    """A thumbnail of a lab report is unreadable; the queue exists to read the
    OPD number off it. The photo must be wrapped in a link the lightbox hooks,
    and that link must still work as a plain new-tab link without JS."""
    client, state = unfiled_client
    _add_row(state)

    body = client.get("/unfiled").text
    assert "data-lightbox" in body, "no hook for the full-page viewer"
    assert 'target="_blank"' in body, "must degrade to opening the raw image without JS"
    assert "initPhotoLightbox" in body, "viewer never initialised on this page"


def test_dismiss_button_warns_that_files_are_deleted(unfiled_client):
    """The button now destroys data -- the confirm must say so."""
    client, state = unfiled_client
    _add_row(state)

    body = client.get("/unfiled").text
    assert "DELETE its photo and transcript" in body
    assert "recycle bin" in body, "say that it is recoverable"


def test_expired_onedrive_token_does_not_500_the_queue(unfiled_client, monkeypatch):
    """OneDriveAuthError is a SIBLING of OneDriveError, not a subclass, so a
    handler catching only the latter let a revoked refresh token take down the
    whole page -- precisely when an operator needs to see what is pending."""
    from app.onedrive import OneDriveAuthError

    client, state = unfiled_client
    _add_row(state)

    async def revoked(*args, **kwargs):
        raise OneDriveAuthError("refresh token revoked -- re-run /oauth/start")

    monkeypatch.setattr(state.onedrive, "download_bytes", revoked)

    resp = client.get("/unfiled")
    assert resp.status_code == 200, "an expired token must not 500 the queue"
    assert "could not load transcript" in resp.text
    assert "Dismiss" in resp.text, "the row itself must still be actionable"


def test_expired_token_while_dismissing_is_reported_not_crashed(unfiled_client, monkeypatch):
    from app.onedrive import OneDriveAuthError

    client, state = unfiled_client
    row_id = _add_row(state)

    async def revoked(*, path):
        raise OneDriveAuthError("refresh token revoked")

    monkeypatch.setattr(state.onedrive, "delete_item", revoked)

    resp = client.post(f"/unfiled/{row_id}/dismiss", follow_redirects=True)
    assert resp.status_code == 200
    assert "could not be deleted" in resp.text


def test_page_actually_loads_the_script_it_calls(unfiled_client):
    """Calling lineAdmin.initPhotoLightbox() without loading admin.js first
    throws ReferenceError and the viewer silently never binds -- the page
    still looks fine, the clicks just do nothing."""
    client, state = unfiled_client
    _add_row(state)

    body = client.get("/unfiled").text
    assert '<script src="/static/admin.js"></script>' in body
    assert body.index("/static/admin.js") < body.index("initPhotoLightbox"), (
        "admin.js must be loaded BEFORE the init call that uses it"
    )


def _parse_forms(html: str):
    """Parse the page with a real HTML parser and return
    {form action: [button elements inside it]}.

    Substring assertions cannot catch a malformed tag: an unterminated
    `<form ...` (a missing '>') still contains the literal text
    'Dismiss & delete files', but the browser folds the following `<button`
    into the form tag as attributes and renders the label as inert text. That
    shipped once -- this parses instead of string-matching.
    """
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms: dict[str, list] = {}
            self._current = None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "form":
                self._current = a.get("action", "")
                self.forms.setdefault(self._current, [])
            elif tag == "button" and self._current is not None:
                self.forms[self._current].append(a)

        def handle_endtag(self, tag):
            if tag == "form":
                self._current = None

    p = _P()
    p.feed(html)
    return p.forms


def test_dismiss_button_is_a_real_clickable_button(unfiled_client):
    client, state = unfiled_client
    row_id = _add_row(state)

    forms = _parse_forms(client.get("/unfiled").text)
    action = f"/unfiled/{row_id}/dismiss"
    assert action in forms, f"no dismiss form parsed; got {list(forms)}"

    buttons = forms[action]
    assert buttons, (
        "the dismiss form contains no <button> element -- the label is inert "
        "text. Check the <form> tag is terminated with '>'."
    )
    assert any(b.get("type") == "submit" for b in buttons)


def test_resolve_button_is_a_real_clickable_button(unfiled_client):
    client, state = unfiled_client
    row_id = _add_row(state)

    forms = _parse_forms(client.get("/unfiled").text)
    buttons = forms.get(f"/unfiled/{row_id}/resolve", [])
    assert any(b.get("type") == "submit" for b in buttons)


def test_no_form_tag_is_left_unterminated(unfiled_client):
    """Guards every form on the page, not just today's two."""
    import re

    client, state = unfiled_client
    _add_row(state)
    html = client.get("/unfiled").text

    assert not re.search(r"<form(?:(?!>)[\s\S])*?<", html), (
        "a <form> tag is missing its closing '>' -- everything after it is "
        "being parsed as attributes"
    )
