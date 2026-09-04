"""A failing OCR backend must not fail *silently*.

MarkItDown catches an exception raised by one converter and moves on to the
next registered one. Its built-in ImageConverter then "succeeds" by reading
EXIF only, so a backend that blows up (expired API key, rate limit, a
missing Tesseract language pack) surfaces as an empty transcript with no OPD
number and, before this was fixed, no error anywhere at all -- the photo
just landed in the unfiled queue with a blank .md and no clue why.

The fallthrough itself is MarkItDown's behaviour and is not something this
app can switch off; what it can do is make sure the real exception is
logged at ERROR before it is swallowed. These tests pin that.
"""

from __future__ import annotations

import io
import logging

import pytest
from markitdown import StreamInfo

from app.ocr.base import ImageOcrConverterBase, LabResult


class ExplodingBackend(ImageOcrConverterBase):
    """Stands in for any backend whose _extract raises at runtime."""

    def _extract(self, image_bytes: bytes, mimetype: str) -> LabResult:
        raise RuntimeError("simulated backend failure: 401 Unauthorized")


class WorkingBackend(ImageOcrConverterBase):
    def _extract(self, image_bytes: bytes, mimetype: str) -> LabResult:
        return LabResult(markdown="ok", opd_number="8258", patient_name=None, confidence=1.0)


def _convert(backend):
    return backend.convert(io.BytesIO(b"\xff\xd8\xff\xe0 not-a-real-jpeg"),
                           StreamInfo(mimetype="image/jpeg", extension=".jpg"))


def test_backend_failure_is_logged_before_it_is_swallowed(caplog):
    with caplog.at_level(logging.ERROR, logger="app.ocr.base"):
        with pytest.raises(RuntimeError, match="simulated backend failure"):
            _convert(ExplodingBackend())

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a failing backend logged nothing -- the failure would be invisible"
    msg = errors[0].getMessage()
    assert "ExplodingBackend" in msg
    assert "EMPTY" in msg and "unfiled" in msg
    # the original traceback must be attached, not just the message
    assert errors[0].exc_info is not None


def test_backend_failure_still_propagates():
    """The exception must be re-raised, not converted into a bogus success."""
    with pytest.raises(RuntimeError):
        _convert(ExplodingBackend())


def test_successful_backend_logs_no_error(caplog):
    with caplog.at_level(logging.ERROR, logger="app.ocr.base"):
        result = _convert(WorkingBackend())
    assert "8258" in result.markdown
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
