"""Shared base converter and result type for the OCR backends.

`LabResult` is the structured output of any backend. Because MarkItDown's
`DocumentConverterResult` carries only a plain markdown string, the
structured fields (opd_number, patient_name, confidence) are embedded as a
small machine-readable JSON block at the very top of the markdown text --
`LabResult.to_markdown()` writes it, `LabResult.from_markdown()` parses it
back out. `app.extract` calls `from_markdown()` on whatever
`MarkItDown.convert()` returns, so this round-trip is the single source of
truth and there is no reliance on converter-instance state.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from dataclasses import dataclass
from typing import Any, BinaryIO, Optional

from markitdown import DocumentConverter, DocumentConverterResult, StreamInfo

ACCEPTED_MIME_PREFIXES = ("image/jpeg", "image/png")
ACCEPTED_EXTENSIONS = (".jpg", ".jpeg", ".png")

logger = logging.getLogger(__name__)

_FRONT_MATTER_START = "<!--LABRESULT-JSON"
_FRONT_MATTER_END = "LABRESULT-JSON-->"


@dataclass
class LabResult:
    """The structured result of extracting one lab-report photo."""

    markdown: str
    opd_number: Optional[str] = None
    patient_name: Optional[str] = None
    confidence: Optional[float] = None

    def to_markdown(self) -> str:
        """Render this result as a single markdown string: a front-matter
        JSON block (for `from_markdown` to recover the structured fields)
        followed by the transcribed report body.
        """
        meta = {
            "opd_number": self.opd_number,
            "patient_name": self.patient_name,
            "confidence": self.confidence,
        }
        front_matter = f"{_FRONT_MATTER_START}\n{json.dumps(meta, ensure_ascii=False)}\n{_FRONT_MATTER_END}\n\n"
        return front_matter + self.markdown

    @classmethod
    def from_markdown(cls, markdown: str) -> "LabResult":
        """Parse a LabResult back out of text produced by `to_markdown()`.

        If no front-matter block is present -- e.g. the tesseract backend,
        which has no structured fields to report -- returns a LabResult with
        only `markdown` populated (the whole string).
        """
        if not markdown.startswith(_FRONT_MATTER_START):
            return cls(markdown=markdown)

        end = markdown.find(_FRONT_MATTER_END)
        if end == -1:
            return cls(markdown=markdown)

        json_blob = markdown[len(_FRONT_MATTER_START) : end].strip()
        body = markdown[end + len(_FRONT_MATTER_END) :].lstrip("\n")

        try:
            meta = json.loads(json_blob)
        except json.JSONDecodeError:
            return cls(markdown=body)

        return cls(
            markdown=body,
            opd_number=meta.get("opd_number"),
            patient_name=meta.get("patient_name"),
            confidence=meta.get("confidence"),
        )


def guess_mimetype(extension: Optional[str]) -> str:
    """Best-effort mimetype guess from a file extension, defaulting to
    image/jpeg (the common case for LINE photo messages).
    """
    if extension:
        guessed, _ = mimetypes.guess_type("_dummy" + extension)
        if guessed:
            return guessed
    return "image/jpeg"


class ImageOcrConverterBase(DocumentConverter):
    """Base MarkItDown DocumentConverter for the OCR backends.

    Subclasses implement `_extract(image_bytes, mimetype) -> LabResult`;
    this base class handles the `accepts`/`convert` DocumentConverter
    interface, the jpeg/png accept check, and rendering the LabResult back
    into the DocumentConverterResult MarkItDown expects.
    """

    def accepts(self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()
        if extension in ACCEPTED_EXTENSIONS:
            return True
        return any(mimetype.startswith(prefix) for prefix in ACCEPTED_MIME_PREFIXES)

    def convert(self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any) -> DocumentConverterResult:
        cur_pos = file_stream.tell()
        try:
            image_bytes = file_stream.read()
        finally:
            file_stream.seek(cur_pos)

        mimetype = stream_info.mimetype or guess_mimetype(stream_info.extension)
        try:
            result = self._extract(image_bytes, mimetype)
        except Exception:
            # MarkItDown catches a converter exception and moves on to the
            # next registered converter. Its built-in ImageConverter then
            # "succeeds" by reading EXIF only, so a failed backend surfaces
            # as an empty transcript with no OPD number and NO error
            # anywhere -- the photo just lands in the unfiled queue with a
            # blank .md. Log it here, where the real exception still
            # exists, before re-raising into that fallthrough.
            logger.exception(
                "OCR backend %s failed on a %s image; MarkItDown will fall back to its "
                "built-in EXIF-only converter, so this photo will produce an EMPTY "
                "transcript and go to the unfiled queue.",
                type(self).__name__,
                mimetype,
            )
            raise
        return DocumentConverterResult(markdown=result.to_markdown())

    def _extract(self, image_bytes: bytes, mimetype: str) -> LabResult:
        raise NotImplementedError("Subclasses must implement _extract()")
