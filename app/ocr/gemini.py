"""OCR backend backed by Google's Gemini API, using the `google-genai` SDK.

`google.genai` is imported lazily inside `__init__`, not at module top
level, so that selecting a different OCR_BACKEND does not require the
`google-genai` package to be importable.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .base import ImageOcrConverterBase, LabResult
from .gemini_schema import to_gemini_schema
from .prompt import PROMPT, RESPONSE_SCHEMA

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

logger = logging.getLogger(__name__)


class GeminiOcrConverter(ImageOcrConverterBase):
    """Extracts lab-report text and structured fields using Gemini."""

    def __init__(self, settings: "Settings"):
        from google import genai  # lazy: only required when OCR_BACKEND=gemini

        if not settings.gemini_api_key:
            raise RuntimeError("GeminiOcrConverter requires GEMINI_API_KEY to be set.")

        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_model
        # Translated once here rather than per request. RESPONSE_SCHEMA is
        # standard JSON Schema shared with the Claude backend; Gemini accepts
        # only a subset of it and rejects the rest server-side. See
        # app/ocr/gemini_schema.py.
        self._response_schema = to_gemini_schema(RESPONSE_SCHEMA)

    def _extract(self, image_bytes: bytes, mimetype: str) -> LabResult:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self._model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mimetype),
                PROMPT,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=self._response_schema,
            ),
        )

        text = response.text
        if text is None:
            raise RuntimeError("Gemini response contained no text to parse as JSON.")

        data = json.loads(text)  # never string-match the model's output

        return LabResult(
            markdown=data["markdown"],
            opd_number=data.get("opd_number"),
            patient_name=data.get("patient_name"),
            confidence=data.get("confidence"),
        )
