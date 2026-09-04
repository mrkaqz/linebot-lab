"""MarkItDown wiring and OPD-number resolution.

`build_markitdown()` creates ONE MarkItDown instance at startup and
registers the configured OCR backend at priority -1. Priority matters: the
built-in ImageConverter registers at priority 0 (PRIORITY_SPECIFIC_FILE_FORMAT),
and MarkItDown tries converters in ascending priority order, so a converter
registered at priority -1 is tried *before* the built-in one. Registering at
the default priority (0) would let the built-in ImageConverter win the
"accepts()" race for every jpeg/png and silently return EXIF-only output with
no OPD number -- see the module docstring in ocr/base.py for the full
DocumentConverter contract.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from markitdown import DocumentConverter, DocumentConverterResult, MarkItDown, StreamInfo

from .config import Settings
from .ocr import build_backend
from .ocr.base import LabResult

logger = logging.getLogger(__name__)

# Lower than the built-in converters' default (PRIORITY_SPECIFIC_FILE_FORMAT
# == 0), so our custom converter is tried first for every image.
CUSTOM_CONVERTER_PRIORITY = -1


@dataclass
class ExtractionResult:
    """The result of extracting one lab-report photo, with the OPD number
    resolved from the LLM output and/or the regex fallback.
    """

    markdown: str
    opd_number: Optional[str]
    patient_name: Optional[str]
    confidence: Optional[float]


class _NotConfiguredConverter(DocumentConverter):
    """Registered instead of the real OCR backend when the selected backend
    (claude/gemini) can't be constructed -- a missing API key, most likely.

    Nothing is required to boot (see app.config.Settings), so
    `build_markitdown` cannot let a backend constructor failure propagate
    and take the process down. Registering this placeholder instead means:
    the app still starts, MarkItDown still has *a* converter registered at
    the OCR priority (so the built-in ImageConverter's EXIF-only fallback
    doesn't silently win), and the failure only surfaces when a photo
    actually needs OCR -- as a clean per-job failure
    (`app.pipeline.process_image_event` already treats an extraction
    exception as "file to _UNFILED", not a crash) rather than a boot crash
    or an unhandled worker exception.
    """

    def __init__(self, reason: str):
        self._reason = reason

    def accepts(self, file_stream, stream_info: StreamInfo, **kwargs) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()
        if extension in (".jpg", ".jpeg", ".png"):
            return True
        return mimetype.startswith("image/jpeg") or mimetype.startswith("image/png")

    def convert(self, file_stream, stream_info: StreamInfo, **kwargs) -> DocumentConverterResult:
        raise RuntimeError(f"OCR backend is not configured: {self._reason}")


def build_markitdown(settings: Settings) -> MarkItDown:
    """Build the process-wide MarkItDown instance, with the configured OCR
    backend registered ahead of the built-ins. Call this at startup and
    again on every OCR-related hot reload (see app.runtime.AppState).

    If the selected backend can't be constructed (e.g. OCR_BACKEND=claude
    with no ANTHROPIC_API_KEY yet), a placeholder converter is registered
    instead and a WARNING is logged -- see `_NotConfiguredConverter`. This
    is reported as a checklist item (Settings.missing_requirements()), not
    a startup crash.
    """
    md = MarkItDown()
    try:
        backend: DocumentConverter = build_backend(settings)
    except Exception as exc:
        logger.warning(
            "OCR backend '%s' could not be initialized (%s) -- finish setting it up in the admin UI "
            "(Setup > OCR). Photos will fail cleanly to the unfiled queue until then.",
            settings.ocr_backend,
            exc,
        )
        backend = _NotConfiguredConverter(str(exc))
    md.register_converter(backend, priority=CUSTOM_CONVERTER_PRIORITY)
    logger.info("MarkItDown ready with OCR_BACKEND=%s registered at priority=%d", settings.ocr_backend, CUSTOM_CONVERTER_PRIORITY)
    return md


def normalize_opd(raw: str) -> str:
    """Normalise a captured/returned OPD number by stripping '-' and '/'."""
    return raw.replace("-", "").replace("/", "")


def find_opd_regex(text: str, pattern: str) -> Optional[str]:
    """Search `text` for the first OPD-number match of `pattern`, returning
    the normalised capture group, or None if no match.
    """
    match = re.search(pattern, text)
    if not match:
        return None
    return normalize_opd(match.group(1))


def resolve_opd(llm_opd: Optional[str], regex_opd: Optional[str]) -> Optional[str]:
    """Resolve the final OPD number from the two candidate sources.

    Precedence: if both the LLM and the regex found a number, prefer the
    LLM's answer but log a warning if they disagree. If only one source has
    an answer, use it. If neither does, return None (caller files as
    unfiled).
    """
    if llm_opd and regex_opd:
        if llm_opd != regex_opd:
            logger.warning("OPD number mismatch: LLM=%s regex=%s -- using LLM value", llm_opd, regex_opd)
        return llm_opd
    return llm_opd or regex_opd


def extract(md: MarkItDown, image_path: str, settings: Settings) -> ExtractionResult:
    """Run MarkItDown (and therefore the configured OCR backend) on the
    image at `image_path`, and resolve the OPD number.
    """
    result = md.convert(image_path)
    lab_result = LabResult.from_markdown(result.markdown)

    regex_opd = find_opd_regex(lab_result.markdown, settings.opd_regex)
    llm_opd = normalize_opd(lab_result.opd_number) if lab_result.opd_number else None

    opd_number = resolve_opd(llm_opd, regex_opd)

    return ExtractionResult(
        markdown=lab_result.markdown,
        opd_number=opd_number,
        patient_name=lab_result.patient_name,
        confidence=lab_result.confidence,
    )
