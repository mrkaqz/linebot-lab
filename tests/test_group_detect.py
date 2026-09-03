"""Group auto-detect: while listening, the webhook handler records the
groupId of the next message from ANY group -- without bypassing signature
verification, and without filing anything from an unconfigured group.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _post_webhook(client, secret: str, events: list[dict]):
    body = json.dumps({"events": events}).encode("utf-8")
    signature = _sign(secret, body)
    return client.post(
        "/line/webhook",
        content=body,
        headers={"x-line-signature": signature, "content-type": "application/json"},
    )


def _image_event(group_id: str, message_id: str = "m1", timestamp: int = 1756900000000) -> dict:
    return {
        "type": "message",
        "message": {"id": message_id, "type": "image"},
        "timestamp": timestamp,
        "source": {"type": "group", "groupId": group_id},
    }


def test_detect_records_group_id_from_unconfigured_group_without_filing(public_client_factory):
    client, state = public_client_factory(line_lab_group_id="Cconfigured000000000000000000000")
    state.group_detect.start()
    assert state.group_detect.is_listening() is True

    response = _post_webhook(
        client, state.settings.line_channel_secret, [_image_event("Cstranger0000000000000000000000")]
    )

    assert response.status_code == 200
    assert state.group_detect.group_id == "Cstranger0000000000000000000000"
    # Nothing was filed/queued -- detection only recorded the id, the
    # message came from a group that isn't the configured one.
    assert state.queue.qsize() == 0


def test_detect_stops_listening_once_a_group_is_found(public_client_factory):
    client, state = public_client_factory(line_lab_group_id="Cconfigured000000000000000000000")
    state.group_detect.start()

    _post_webhook(client, state.settings.line_channel_secret, [_image_event("Cfirst00000000000000000000000000")])
    assert state.group_detect.is_listening() is False
    assert state.group_detect.group_id == "Cfirst00000000000000000000000000"

    # A second, different group posting afterwards must NOT overwrite the
    # already-found id -- detection already stopped.
    _post_webhook(client, state.settings.line_channel_secret, [_image_event("Csecond0000000000000000000000000")])
    assert state.group_detect.group_id == "Cfirst00000000000000000000000000"


def test_detect_does_not_run_when_not_listening(public_client_factory):
    client, state = public_client_factory(line_lab_group_id="Cconfigured000000000000000000000")
    assert state.group_detect.is_listening() is False

    _post_webhook(client, state.settings.line_channel_secret, [_image_event("Cwhoever000000000000000000000000")])

    assert state.group_detect.group_id is None


def test_detect_never_bypasses_signature_verification(public_client_factory):
    client, state = public_client_factory(line_lab_group_id="Cconfigured000000000000000000000")
    state.group_detect.start()

    body = json.dumps({"events": [_image_event("Cstranger0000000000000000000000")]}).encode("utf-8")
    response = client.post(
        "/line/webhook",
        content=body,
        headers={"x-line-signature": "not-a-valid-signature", "content-type": "application/json"},
    )

    assert response.status_code == 400
    assert state.group_detect.group_id is None


def test_configured_group_still_files_normally_while_detect_is_idle(public_client_factory, monkeypatch):
    client, state = public_client_factory(line_lab_group_id="Cconfigured000000000000000000000")

    response = _post_webhook(
        client, state.settings.line_channel_secret, [_image_event("Cconfigured000000000000000000000")]
    )

    assert response.status_code == 200
    assert state.queue.qsize() == 1
