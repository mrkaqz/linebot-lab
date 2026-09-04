"""LINE Messaging API client: webhook signature verification, content
download, and reply/push messages.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from pathlib import Path
from typing import Final, Optional

import httpx

logger = logging.getLogger(__name__)

_CONTENT_URL: Final = "https://api-data.line.me/v2/bot/message/{message_id}/content"
_REPLY_URL: Final = "https://api.line.me/v2/bot/message/reply"
_PUSH_URL: Final = "https://api.line.me/v2/bot/message/push"
_GROUP_SUMMARY_URL: Final = "https://api.line.me/v2/bot/group/{group_id}/summary"

_MAX_DOWNLOAD_RETRIES: Final = 4
_RETRY_BASE_DELAY_SECONDS: Final = 1.0


class LineAuthError(RuntimeError):
    """LINE rejected our credentials (401/403).

    Raised instead of a bare httpx.HTTPStatusError so the failure names the
    cause and the fix rather than surfacing as a stack trace ending in
    `raise_for_status()`. This is ALWAYS the channel access token, never the
    channel secret: the secret only signs the inbound webhook, while every
    outbound call authenticates with `Authorization: Bearer <access token>`.
    A webhook that returns 200 while these calls 401 is exactly that split.
    """


def secret_fingerprint(channel_secret: Optional[str]) -> str:
    """A short, non-reversible fingerprint of a channel secret, safe to log.

    Exists so an operator can answer "is the secret this app is using the same
    one the LINE console shows?" without the secret ever being written to a log
    file. Compare the value logged here against the console's secret run
    through the same function -- README "LINE webhook Verify returns 400" gives
    the one-liner.
    """
    if not channel_secret:
        return "<unset>"
    return hashlib.sha256(channel_secret.encode("utf-8")).hexdigest()[:8]


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    """Verify the `x-line-signature` header: base64(HMAC-SHA256(channel_secret, body)).

    `body` MUST be the raw request bytes read before any JSON parsing --
    re-serializing the parsed JSON changes the bytes and breaks the
    signature check.
    """
    if not signature:
        return False
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


class LineClient:
    """Thin async wrapper around the parts of the LINE Messaging API this
    bot needs: content download, reply, and push.
    """

    def __init__(self, channel_access_token: str, client: httpx.AsyncClient | None = None):
        # Strip whitespace: a channel access token pasted from the LINE
        # console easily picks up a trailing newline, which makes the
        # Authorization header malformed and yields an indistinguishable 401.
        self._token = (channel_access_token or "").strip()
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def download_content(self, message_id: str, dest_path: Path) -> None:
        """Stream a message's binary content (e.g. an image) to `dest_path`.

        LINE only retains message content for a limited window after it is
        sent, so callers should download immediately, before doing any
        (possibly slow) OCR work. Retries on 5xx responses and connection
        errors with exponential backoff.
        """
        url = _CONTENT_URL.format(message_id=message_id)
        last_exc: Exception | None = None

        for attempt in range(_MAX_DOWNLOAD_RETRIES):
            try:
                async with self._client.stream("GET", url, headers=self._auth_headers()) as response:
                    if response.status_code >= 500:
                        response.raise_for_status()
                    response.raise_for_status()
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest_path, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403):
                    # Retrying cannot help, and the generic 4xx path would
                    # surface this as a raw traceback. Name the credential --
                    # and distinguish "no token at all" from "token
                    # rejected", because an unset token builds an
                    # `Authorization: Bearer ` header that LINE answers with
                    # exactly the same 401 as a wrong one.
                    if not self._token:
                        raise LineAuthError(
                            f"No LINE channel access token is configured, so message {message_id} "
                            f"could not be downloaded (LINE returned {exc.response.status_code} to an "
                            "empty Bearer token). This is a MISSING setting, not a wrong one: set it "
                            "under Setup > LINE. The dashboard's setup checklist lists it as "
                            "'LINE channel access token' while it is unset. Note that saving the "
                            "Setup > LINE form with the token box left blank leaves the stored value "
                            "untouched -- the box always renders empty, so it has to be pasted in "
                            "explicitly, including when you are only saving a detected group id."
                        ) from exc
                    raise LineAuthError(
                        f"LINE rejected the channel access token ({exc.response.status_code}) when "
                        f"downloading message {message_id}. A token IS configured, so it is wrong, "
                        "expired, or from a different channel -- re-issue it in the LINE console "
                        "(Messaging API > Channel access token) and paste it into Setup > LINE. Note "
                        "this is the ACCESS TOKEN, not the channel secret: the secret only verifies "
                        "inbound webhooks, and yours is already working if the webhook returned 200."
                    ) from exc
                if exc.response.status_code < 500:
                    raise  # other 4xx: not retryable
                last_exc = exc
            except httpx.TransportError as exc:
                last_exc = exc

            delay = _RETRY_BASE_DELAY_SECONDS * (2**attempt)
            logger.warning(
                "download_content retry %d/%d for message %s after error: %s",
                attempt + 1,
                _MAX_DOWNLOAD_RETRIES,
                message_id,
                last_exc,
            )
            await asyncio.sleep(delay)

        assert last_exc is not None
        raise last_exc

    async def reply(self, reply_token: str, text: str) -> None:
        payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
        response = await self._client.post(_REPLY_URL, headers=self._auth_headers(), json=payload)
        response.raise_for_status()

    async def push(self, to: str, text: str) -> None:
        payload = {"to": to, "messages": [{"type": "text", "text": text}]}
        response = await self._client.post(_PUSH_URL, headers=self._auth_headers(), json=payload)
        response.raise_for_status()

    async def get_group_summary(self, group_id: str) -> Optional[dict]:
        """Best-effort: the group's display name/picture, for the setup
        UI's "Detect group" flow. Returns None (never raises) if the group
        summary isn't reachable -- some groups aren't, and the id alone is
        still useful without a name.
        """
        try:
            response = await self._client.get(
                _GROUP_SUMMARY_URL.format(group_id=group_id), headers=self._auth_headers()
            )
            if response.status_code != 200:
                return None
            return response.json()
        except httpx.HTTPError:
            return None
