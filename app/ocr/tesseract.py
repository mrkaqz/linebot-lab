"""Local, offline OCR backend using Tesseract (via pytesseract).

Unlike the LLM backends, this backend cannot answer "what is the OPD
number" -- it can only produce raw text. `opd_number`/`patient_name`/
`confidence` are left None; `app.extract`'s OPD_REGEX is the only source of
the OPD number when this backend is selected.

`pytesseract`/`PIL` are imported lazily inside `__init__` for consistency
with the other backends, though in practice they are always installed
(requirements.txt lists them unconditionally since this is the offline
default/fallback backend).
"""

from __future__ import annotations

import importlib
import io
import logging
from typing import TYPE_CHECKING

from .base import ImageOcrConverterBase, LabResult

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

logger = logging.getLogger(__name__)

TESSERACT_LANG = "tha+eng"


class TesseractOcrConverter(ImageOcrConverterBase):
    """Extracts lab-report text using local Tesseract OCR (no LLM, no
    network). Requires the tesseract-ocr and tesseract-ocr-tha system
    packages (see Dockerfile).
    """

    def __init__(self, settings: "Settings"):
        importlib.import_module("pytesseract")  # fail fast if not installed
        self._settings = settings

    def _extract(self, image_bytes: bytes, mimetype: str) -> LabResult:
        import pytesseract
        from PIL import Image, ImageOps

        image = Image.open(io.BytesIO(image_bytes))
        image = ImageOps.grayscale(image)
        image = ImageOps.autocontrast(image)
        # Simple fixed threshold binarization -- improves OCR on typical
        # phone photos of printed lab reports.
        image = image.point(lambda p: 255 if p > 150 else 0)

        text = pytesseract.image_to_string(image, lang=TESSERACT_LANG)

        return LabResult(markdown=text, opd_number=None, patient_name=None, confidence=None)
