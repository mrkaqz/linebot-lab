"""OCR backend backed by the Anthropic API (Claude), using the official
`anthropic` SDK (not an OpenAI-compatible shim).

The `anthropic` package is imported lazily inside `__init__`, not at module
top level, so that selecting a different OCR_BACKEND does not require the
`anthropic` package to be importable.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING

from .base import ImageOcrConverterBase, LabResult
from .prompt import PROMPT, RESPONSE_SCHEMA

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

logger = logging.getLogger(__name__)


class ClaudeOcrConverter(ImageOcrConverterBase):
    """Extracts lab-report text and structured fields using Claude."""

    def __init__(self, settings: "Settings"):
        import anthropic  # lazy: only required when OCR_BACKEND=claude

        if not settings.anthropic_api_key:
            raise RuntimeError("ClaudeOcrConverter requires ANTHROPIC_API_KEY to be set.")

        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.claude_model

    def _extract(self, image_bytes: bytes, mimetype: str) -> LabResult:
        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

        response = self._client.messages.create(
            model=self._model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": RESPONSE_SCHEMA,
                }
            },
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mimetype,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
        )

        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise RuntimeError("Claude response contained no text block to parse as JSON.")

        data = json.loads(text)  # never string-match the model's output

        return LabResult(
            markdown=data["markdown"],
            opd_number=data.get("opd_number"),
            patient_name=data.get("patient_name"),
            confidence=data.get("confidence"),
        )
