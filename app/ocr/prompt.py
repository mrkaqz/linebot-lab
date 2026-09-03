"""The single prompt and JSON schema shared by both LLM OCR backends
(claude.py and gemini.py), so they can never drift apart.

Both backends ask the model to (a) transcribe the lab report faithfully into
Markdown, preserving the test-name / result / unit / reference-range table
structure, and (b) separately return the OPD number, patient name, and a
0-1 confidence, as one JSON object matching RESPONSE_SCHEMA.
"""

from __future__ import annotations

PROMPT = """\
You are transcribing a photograph of a Thai hospital or clinic blood-test \
(lab) report. The report text may be in Thai, English, or a mix of both.

Do the following:

1. Transcribe the report FAITHFULLY into Markdown. Preserve the table \
structure of the test results: one row per test, with columns for the test \
name, result value, unit, and reference range, in that order, using a \
Markdown table. Keep Thai text as Thai -- do not translate it. Include every \
test row, even ones that look normal or unremarkable. Include any other \
printed information (patient details, dates, lab/hospital name) as plain \
text above the table.

2. Separately extract these fields:
   - opd_number: the patient's OPD number, sometimes labelled "OPD No.", \
"O.P.D.", "HN", or in Thai "เลขที่ผู้ป่วยนอก" / "เลขที่". Return exactly the \
digits and separators as printed (e.g. "12345" or "64-001234"). Return null \
if you cannot find one.
   - patient_name: the patient's full name as printed on the report, or \
null if it is not present.
   - confidence: your own confidence, from 0.0 to 1.0, that opd_number is \
correct and that the transcription is complete and accurate.

Respond with ONLY a JSON object matching the provided schema. Do not include \
any commentary outside the JSON.
"""

# Standard JSON Schema (draft 2020-12 subset). Used verbatim as:
#   - Anthropic: output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}}
#   - Gemini: GenerateContentConfig(response_mime_type="application/json", response_schema=RESPONSE_SCHEMA)
RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "markdown": {
            "type": "string",
            "description": (
                "The lab report transcribed to Markdown, preserving the "
                "test name / result / unit / reference-range table."
            ),
        },
        "opd_number": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "The patient's OPD number/HN as printed, or null if not found.",
        },
        "patient_name": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "The patient's full name as printed, or null if not found.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence (0-1) in the opd_number and transcription accuracy.",
        },
    },
    "required": ["markdown", "opd_number", "patient_name", "confidence"],
    "additionalProperties": False,
}
