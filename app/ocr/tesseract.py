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
        from PIL import Image

        # Deliberately NO preprocessing: the image goes to Tesseract as it
        # arrived.
        #
        # This used to grayscale, autocontrast, and then binarize at a fixed
        # threshold of 150, on the theory that it helps "typical phone photos
        # of printed lab reports". Measured against the real reports the lab
        # sends -- which are screenshots/PDF exports, not photos of paper --
        # every one of those steps made things worse, and the fixed threshold
        # was the worst of the six variants tried: it broke the right-hand
        # header column onto separate lines, stranding "HN Hospital/Clinic"
        # from its value so the field-anchored OPD_REGEX could no longer
        # match it. Passing the image through untouched was the only variant
        # that read every header field on both reports.
        #
        # The reason is that Tesseract already binarizes internally, using
        # adaptive (Otsu) thresholding. Handing it an image flattened by a
        # naive global threshold destroys the gradients that algorithm needs
        # and cannot outperform it. If a genuinely poor phone photo ever
        # needs help, add targeted preprocessing behind a check rather than
        # unconditionally -- and measure it against real reports first, with
        # scripts/ocr_check.py.
        image = Image.open(io.BytesIO(image_bytes))

        text = pytesseract.image_to_string(image, lang=TESSERACT_LANG)

        return LabResult(markdown=text, opd_number=None, patient_name=None, confidence=None)
