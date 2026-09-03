"""Converter priority: a custom OCR converter registered at priority=-1
must win over the built-in ImageConverter for a jpeg, since MarkItDown
tries converters in ascending priority order and the built-in ImageConverter
registers at priority 0.
"""

from __future__ import annotations

from markitdown import MarkItDown

from app.ocr.base import ImageOcrConverterBase, LabResult
from app.extract import CUSTOM_CONVERTER_PRIORITY


class _StubOcrConverter(ImageOcrConverterBase):
    """A minimal OCR converter standing in for a real backend."""

    def _extract(self, image_bytes: bytes, mimetype: str) -> LabResult:
        return LabResult(markdown="STUB TRANSCRIPT", opd_number="99999", patient_name="Test Patient", confidence=0.99)


def test_custom_priority_is_negative():
    # Lower than the built-ins' PRIORITY_SPECIFIC_FILE_FORMAT (0), so it is
    # tried first.
    assert CUSTOM_CONVERTER_PRIORITY < 0


def test_custom_converter_wins_over_builtin_image_converter(tmp_path):
    md = MarkItDown()
    md.register_converter(_StubOcrConverter(), priority=CUSTOM_CONVERTER_PRIORITY)

    jpg_path = tmp_path / "photo.jpg"
    # Content doesn't need to be a real JPEG: the built-in ImageConverter's
    # accepts() only inspects the extension/mimetype, and our stub should
    # win the race before the built-in is ever tried.
    jpg_path.write_bytes(b"not-real-jpeg-bytes-but-right-extension")

    result = md.convert(str(jpg_path))

    parsed = LabResult.from_markdown(result.markdown)
    assert parsed.markdown == "STUB TRANSCRIPT"
    assert parsed.opd_number == "99999"


def test_builtin_image_converter_alone_returns_no_opd_metadata(tmp_path):
    """Sanity check for the CRITICAL FACT this module exists to guard
    against: MarkItDown's built-in ImageConverter does not OCR at all --
    with no custom converter registered, converting a jpeg yields only
    (possibly empty) EXIF-derived text, never our front-matter block.
    """
    md = MarkItDown()  # no custom converter registered

    jpg_path = tmp_path / "photo.jpg"
    jpg_path.write_bytes(b"not-real-jpeg-bytes-but-right-extension")

    result = md.convert(str(jpg_path))

    assert not result.markdown.startswith("<!--LABRESULT-JSON")
