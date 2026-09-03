"""Backend factory: builds the DocumentConverter selected by OCR_BACKEND.

Each backend module imports its SDK lazily (inside __init__), so only the
selected backend's dependency needs to actually be importable/configured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from markitdown import DocumentConverter

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings


def build_backend(settings: "Settings") -> DocumentConverter:
    """Instantiate the DocumentConverter for `settings.ocr_backend`."""
    if settings.ocr_backend == "claude":
        from .claude import ClaudeOcrConverter

        return ClaudeOcrConverter(settings)
    if settings.ocr_backend == "gemini":
        from .gemini import GeminiOcrConverter

        return GeminiOcrConverter(settings)
    if settings.ocr_backend == "tesseract":
        from .tesseract import TesseractOcrConverter

        return TesseractOcrConverter(settings)

    raise ValueError(f"Unknown OCR_BACKEND: {settings.ocr_backend!r}")
