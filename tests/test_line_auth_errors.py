"""A bad LINE channel access token must be diagnosable.

The channel SECRET and the channel ACCESS TOKEN are different credentials
doing different jobs, and confusing them costs real debugging time:

  - the secret signs the INBOUND webhook (verify_signature)
  - the access token authenticates every OUTBOUND call, as
    `Authorization: Bearer <token>`

So a deployment can have a perfectly good secret -- webhook returns 200 --
while every content download 401s. That combination is not a contradiction,
it is the signature of a bad access token, and the error message says so.

Separately, a failure in the worker means the photo is gone: the webhook
already returned 200 and the message id was marked processed before the work
started, so LINE never retries. Such a drop is recorded to the activity log
rather than living only in the container log.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.line_client import LineAuthError, LineClient


def _client_returning(status: int) -> LineClient:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json={"message": "denied"})
    )
    return LineClient("a-token", client=httpx.AsyncClient(transport=transport))


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_raises_a_named_error(status, tmp_path):
    client = _client_returning(status)
    with pytest.raises(LineAuthError) as exc:
        await client.download_content("msg-1", tmp_path / "out.jpg")
    await client.aclose()

    msg = str(exc.value)
    assert "access token" in msg.lower()
    assert "Setup > LINE" in msg
    # must steer away from the wrong credential
    assert "not the channel" in msg.lower()


async def test_auth_failure_is_not_retried(tmp_path):
    """Retrying a 401 cannot help and just delays the error."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={})

    client = LineClient("t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(LineAuthError):
        await client.download_content("msg-1", tmp_path / "out.jpg")
    await client.aclose()
    assert len(calls) == 1


async def test_other_4xx_still_raises_the_plain_http_error(tmp_path):
    """Only 401/403 get the credential-specific message."""
    client = _client_returning(404)
    with pytest.raises(httpx.HTTPStatusError):
        await client.download_content("msg-1", tmp_path / "out.jpg")
    await client.aclose()


async def test_token_whitespace_is_stripped():
    """A token pasted from the console easily carries a trailing newline,
    which makes the Authorization header malformed and 401s identically."""
    client = LineClient("  a-token\n")
    assert client._auth_headers()["Authorization"] == "Bearer a-token"
    await client.aclose()


def test_secret_and_token_are_independent_credentials():
    """Guards the mental model the error message depends on: verifying a
    webhook signature uses the secret and never the access token."""
    import base64
    import hashlib
    import hmac

    from app.line_client import verify_signature

    secret, body = "the-secret", b'{"events":[]}'
    sig = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()

    assert verify_signature(secret, body, sig) is True
    assert verify_signature("an-access-token", body, sig) is False


async def test_worker_records_a_dropped_event_in_the_activity_log(make_app_state, monkeypatch):
    """The photo is unrecoverable once this happens -- it must at least be
    visible somewhere an operator looks."""
    import app.main as main_mod

    state = make_app_state()
    state.queue = asyncio.Queue()

    async def boom(*args, **kwargs):
        raise LineAuthError("LINE rejected the channel access token (401)")

    monkeypatch.setattr(main_mod, "process_image_event", boom)

    await state.queue.put({"message": {"id": "msg-dropped", "type": "image"}})
    task = asyncio.create_task(main_mod._worker(state))
    await state.queue.join()
    task.cancel()

    rows = [dict(r) for r in state.store.recent_activity(limit=10)]
    dropped = [r for r in rows if r["kind"] == "error"]
    assert dropped, f"no error activity recorded; got {rows}"
    assert "msg-dropped" in dropped[0]["detail"]
    assert "LineAuthError" in dropped[0]["detail"]


async def test_worker_survives_the_failure_and_keeps_draining(make_app_state, monkeypatch):
    """One bad event must not kill the worker for every later photo."""
    import app.main as main_mod

    state = make_app_state()
    state.queue = asyncio.Queue()
    seen = []

    async def sometimes_boom(event, **kwargs):
        seen.append(event["message"]["id"])
        if event["message"]["id"] == "bad":
            raise RuntimeError("nope")

    monkeypatch.setattr(main_mod, "process_image_event", sometimes_boom)

    await state.queue.put({"message": {"id": "bad", "type": "image"}})
    await state.queue.put({"message": {"id": "good", "type": "image"}})
    task = asyncio.create_task(main_mod._worker(state))
    await state.queue.join()
    task.cancel()

    assert seen == ["bad", "good"]
