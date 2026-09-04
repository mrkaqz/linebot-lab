"""The Tesseract backend must hand the image to Tesseract untouched.

It used to grayscale, autocontrast and then binarize at a fixed threshold of
150. Measured against the real reports the lab sends -- screenshots/PDF
exports rather than photos of paper -- that made OCR worse. The fixed
threshold in particular broke the right-hand header column onto separate
lines, stranding "HN Hospital/Clinic" from its value so the field-anchored
OPD_REGEX could no longer reach it; that report then filed only because it
happened to carry an "OPD " prefix the fallback could catch.

Tesseract binarizes internally with adaptive (Otsu) thresholding, so a naive
global threshold destroys information its own algorithm needs. These tests
pin the pass-through so preprocessing cannot quietly return without someone
measuring it first.
"""

from __future__ import annotations

import io

import pytest

from app.config import Settings
from app.ocr.tesseract import TesseractOcrConverter


@pytest.fixture
def gradient_jpeg() -> bytes:
    """An image with many distinct grey levels. Binarization would collapse
    it to exactly two; anything else leaves it varied."""
    from PIL import Image

    img = Image.new("L", (256, 64))
    img.putdata([x % 256 for _ in range(64) for x in range(256)])
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _capture_image_passed_to_tesseract(monkeypatch, image_bytes):
    import pytesseract

    seen = {}

    def fake_image_to_string(image, lang=None, **kwargs):
        seen["image"] = image
        seen["lang"] = lang
        return "stub ocr text"

    monkeypatch.setattr(pytesseract, "image_to_string", fake_image_to_string)
    converter = TesseractOcrConverter(Settings(ocr_backend="tesseract"))
    result = converter._extract(image_bytes, "image/jpeg")
    return seen, result


def test_image_is_not_binarized_before_ocr(monkeypatch, gradient_jpeg):
    seen, _ = _capture_image_passed_to_tesseract(monkeypatch, gradient_jpeg)
    levels = {px for px in seen["image"].convert("L").getdata()}
    assert len(levels) > 2, (
        "the image reached Tesseract with only "
        f"{len(levels)} distinct level(s) -- it was binarized. Tesseract does "
        "its own adaptive thresholding; pre-flattening measurably degraded "
        "OCR on the real reports."
    )


def test_image_keeps_its_original_size(monkeypatch, gradient_jpeg):
    seen, _ = _capture_image_passed_to_tesseract(monkeypatch, gradient_jpeg)
    assert seen["image"].size == (256, 64)


def test_thai_and_english_are_both_requested(monkeypatch, gradient_jpeg):
    """The lab's reports are bilingual; dropping either language loses data.
    Tesseract hard-fails if a requested language pack is missing, which is a
    common cause of an empty transcript -- see test_ocr_failure_logging."""
    seen, _ = _capture_image_passed_to_tesseract(monkeypatch, gradient_jpeg)
    assert seen["lang"] == "tha+eng"


def test_backend_returns_text_and_leaves_structured_fields_unset(monkeypatch, gradient_jpeg):
    """This backend cannot answer "what is the OPD number" -- OPD_REGEX is
    the only source of it when tesseract is selected."""
    _, result = _capture_image_passed_to_tesseract(monkeypatch, gradient_jpeg)
    assert result.markdown == "stub ocr text"
    assert result.opd_number is None
    assert result.patient_name is None
    assert result.confidence is None
