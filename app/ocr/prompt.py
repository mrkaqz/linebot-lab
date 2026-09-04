"""The single prompt and JSON schema shared by both LLM OCR backends
(claude.py and gemini.py), so they can never drift apart.

Both backends ask the model to (a) transcribe the lab report faithfully into
Markdown, preserving the test-name / result / unit / reference-range table
structure, and (b) separately return the OPD number, patient name, and a
0-1 confidence, as one JSON object matching RESPONSE_SCHEMA.

The reports are VETERINARY lab reports. They carry three different id
numbers, and only one of them is the filing key:

    HN VET             : 00234654   <- the LAB's own patient id (8 digits)
    LN VET             : 00350116   <- the LAB's specimen id    (8 digits)
    HN Hospital/Clinic : 8258       <- the CLINIC's id (4 digits) == opd_number

Some reports print the last one with an "OPD " prefix ("OPD 9654") and some
print bare digits ("8258"), so the prefix cannot be relied on as a marker --
the field label is what identifies it. Filing under "HN VET" would put every
report in the wrong folder, so the prompt below calls that out explicitly.
"""

from __future__ import annotations

PROMPT = """\
You are transcribing a photograph of a veterinary lab report from a Thai \
veterinary diagnostic laboratory. The patient is an animal; the report text \
may be in Thai, English, or a mix of both.

Do the following:

1. Transcribe the report FAITHFULLY into Markdown. Preserve the table \
structure of the test results: one row per test, with columns for the test \
name, result value, unit, and reference range, in that order, using a \
Markdown table. Some reports group rows under section headings such as \
[Hematology], [Biochemical], [RBCs Morphology] or [Blood parasite] -- keep \
those headings. Keep any L / H / (R) flag that marks a result as low, high \
or confirmatory-repeated. Keep Thai text as Thai -- do not translate it. \
Include every test row, even ones that look normal or unremarkable. Include \
any other printed information (patient details, dates, lab/clinic name) as \
plain text above the table.

2. Separately extract these fields:
   - opd_number: EXACTLY the 4-digit number printed in the report's \
"HN Hospital/Clinic" header field. This is the number the referring CLINIC \
assigned, and it is the only value wanted here. Some reports print it with \
an "OPD " prefix (e.g. "OPD 9654") and some print the bare digits (e.g. \
"8258") -- in both cases return only the 4 digits, without the prefix. \
Do NOT return the "HN VET" or "LN VET" value: those are the LABORATORY's own \
8-digit identifiers, and filing under one of them would be wrong. If the \
"HN Hospital/Clinic" field is missing or empty, or you cannot read 4 digits \
from it with confidence, return null. Returning null is much better than \
guessing: a wrong number files this patient's report under a different \
patient, whereas null sends it to a queue a human reviews.
   - patient_name: the animal's name as printed in the "Name" field (a Thai \
name such as "ชูใจ"), NOT the owner's name and NOT the clinic name. \
Return null if it is not present.
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
            "description": (
                "The 4 digits of the report's 'HN Hospital/Clinic' field (the "
                "referring clinic's own patient number), without any 'OPD ' "
                "prefix, or null if not readable. Never the 'HN VET' or "
                "'LN VET' value -- those are the laboratory's own ids."
            ),
        },
        "patient_name": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "The animal's name as printed in the 'Name' field, or null if not found.",
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
