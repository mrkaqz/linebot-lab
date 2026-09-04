"""OPD regex: the default OPD_REGEX from app.config, exercised against the
header layout of the real veterinary lab reports this bot files.

Those reports carry three id numbers::

    HN VET             : 00234654   <- the LAB's patient id  (8 digits)
    LN VET             : 00350116   <- the LAB's specimen id (8 digits)
    HN Hospital/Clinic : 8258       <- the CLINIC's id (4 digits) == the OPD number

Only the last one is the filing key. Two properties matter and each has
tests below:

1. The pattern anchors on the "HN Hospital/Clinic" field label, NOT on a
   bare "HN" -- otherwise "HN VET" wins simply by appearing first.
2. The capture is exactly 4 digits, so the lab's 8-digit ids cannot match
   even if OCR mangles the field labels around them.

The "OPD " prefix is present on some reports and absent on others, so both
spellings are covered.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.extract import find_opd_regex, normalize_opd

DEFAULT_PATTERN = Settings.model_fields["opd_regex"].default

# Header block as transcribed from the two real sample reports, in the
# shapes the OCR backends plausibly emit (plain text, and Markdown table).
IMG1_PLAIN = (
    "Hospital/Clinic : ทรายมูลสัตวแพทย์\n"
    "Name : ชูใจ    Age : -   Sex : Fs\n"
    "Species : Feline    Breed : -\n"
    "HN VET : 00234654    HN Hospital/Clinic : 8258\n"
    "LN VET : 00350116    Owner Name : -\n"
    "Received Date : 01-09-2569    Received Time : 12:39\n"
)
IMG2_PLAIN = (
    "Hospital/Clinic : ทรายมูลสัตวแพทย์\n"
    "Name : มีทอง    Age : -   Sex : F\n"
    "Species : Feline    Breed : -\n"
    "HN VET : 00234769    HN Hospital/Clinic : OPD 9654\n"
    "LN VET : 00350320    Owner Name : -\n"
    "Received Date : 02-09-2569    Received Time : 16:33\n"
)

POSITIVE = [
    # The two real reports, end to end.
    (IMG1_PLAIN, "8258"),
    (IMG2_PLAIN, "9654"),
    # Same headers rendered as a Markdown table by the transcribing model.
    ("| HN VET : | 00234654 | HN Hospital/Clinic : | 8258 |", "8258"),
    ("| HN VET : | 00234769 | HN Hospital/Clinic : | OPD 9654 |", "9654"),
    # OCR/spacing variants of the field label and separator.
    ("HN Hospital / Clinic : 8258", "8258"),
    ("HN Hospital/Clinic: 8258", "8258"),
    ("HN Hospital/Clinic 8258", "8258"),
    ("HN Hospital/Clinic : OPD 9654", "9654"),
    # The clinic's number reached even when OCR drops "VET" from the lab's
    # field, which would otherwise leave a bare "HN : <8 digits>" decoy.
    ("HN : 00234654   HN Hospital/Clinic : 8258", "8258"),
    # A bare "OPD nnnn" elsewhere on the page is still a usable fallback.
    ("OPD 9654", "9654"),
    ("OPD No. 8258", "8258"),
]

NEGATIVE = [
    # The lab's own ids must never be captured, alone or together.
    "HN VET : 00234654",
    "LN VET : 00350116",
    "HN VET : 00234654    LN VET : 00350116",
    # Wrong length -> better unfiled than misfiled.
    "HN Hospital/Clinic : 825",
    "HN Hospital/Clinic : 82580",
    # Dates and other stray digits, including the Thai Buddhist-era year.
    "Received Date : 01-09-2569    Received Time : 12:39",
    "Report date: 2026-09-03",
    "Patient age: 45",
    "Temperature 98.6",
    # Prose that merely mentions OPD.
    "OPD services available",
    "no identifiers here",
]


@pytest.mark.parametrize("text,expected", POSITIVE)
def test_positive_matches(text, expected):
    assert find_opd_regex(text, DEFAULT_PATTERN) == expected


@pytest.mark.parametrize("text", NEGATIVE)
def test_negative_matches(text):
    assert find_opd_regex(text, DEFAULT_PATTERN) is None


def test_lab_id_never_wins_over_clinic_id():
    """The decoy "HN VET" appears before the real field on every report, so
    a pattern anchored on a bare "HN" would capture the lab's id instead."""
    assert find_opd_regex(IMG1_PLAIN, DEFAULT_PATTERN) == "8258"
    assert "00234654" not in (find_opd_regex(IMG1_PLAIN, DEFAULT_PATTERN) or "")


def test_opd_prefix_is_optional_and_stripped():
    """Report 1 prints bare digits, report 2 prints an "OPD " prefix; both
    must yield the same 4-digit shape so they file consistently."""
    a = find_opd_regex(IMG1_PLAIN, DEFAULT_PATTERN)
    b = find_opd_regex(IMG2_PLAIN, DEFAULT_PATTERN)
    assert a == "8258" and b == "9654"
    assert a.isdigit() and b.isdigit()
    assert len(a) == len(b) == 4


def test_normalize_strips_dashes_and_slashes():
    assert normalize_opd("64-00/1234") == "64001234"


def test_normalize_leaves_plain_digits_untouched():
    assert normalize_opd("445566") == "445566"
# --- Real Tesseract output -------------------------------------------------
# Verbatim lines produced by tesseract v5 (lang=eng) against the two real
# report photos, both raw and after the binarization app/ocr/tesseract.py
# applies. Captured 2026-09-04. These are ground truth, not guesses, and
# they show two things the hand-written fixtures above did not:
#
#   * Tesseract reads the "l/" of "Hospital/Clinic" as "v", so the real text
#     contains both "HN HospitalClinic" and "HN HospitavClinic". Hence the
#     loose "Hospita.{0,4}Clinic" label in the default pattern.
#   * It misreads the lab's own ids ("00234654" -> "90234654", "00234769" ->
#     "0234769"). Harmless here only because the capture is exactly 4 digits,
#     which those can never satisfy -- see test_lab_id_ocr_damage_is_inert.
REAL_OCR = [
    (
        "Lab1 binarized",
        "hewaee Is. 052-010-509 Tnrans. 052-010-5t9 "
        "HN VET: 90234654 HN HospitalClinic : 8258",
        "8258",
    ),
    (
        "Lab1 raw",
        "miata ie 052-010-509 Imar7. 052-010-519 "
        "HN VET: 90234654 HN HospitalClinic : 8258",
        "8258",
    ),
    (
        "Lab2 raw",
        'awe: We OSZOLGs0s. "Imsaras052 01051 '
        "HN VET: 90234769 HN HospitalClinic : OPD 9654",
        "9654",
    ),
    (
        # Binarization split this report's right-hand column onto its own
        # line, stranding the value two lines below its label. The anchored
        # branch cannot bridge that; only the generic "OPD nnnn" fallback
        # reaches it -- see test_binarized_split_relies_on_opd_fallback.
        "Lab2 binarized (column split)",
        "HN VET: 0234769 HN HospitavClinic\n"
        "LN VET: 06350320 Owner Name: -\n"
        ": OPD 9654",
        "9654",
    ),
]

ANCHORED_ONLY = (
    r"(?i)\bHN\s*Hospita.{0,4}Clinic\s*[:.#\-]?\s*\|?\s*(?:OPD\s*)?([0-9]{4})\b"
)


@pytest.mark.parametrize(
    "label,text,expected", REAL_OCR, ids=[c[0] for c in REAL_OCR]
)
def test_real_tesseract_output(label, text, expected):
    assert find_opd_regex(text, DEFAULT_PATTERN) == expected


@pytest.mark.parametrize(
    "label,text,expected", REAL_OCR, ids=[c[0] for c in REAL_OCR]
)
def test_lab_id_ocr_damage_is_inert(label, text, expected):
    """Tesseract mangles the lab's 8-digit ids, but the 4-digit capture means
    no mangling of them can ever produce a filed folder name."""
    got = find_opd_regex(text, DEFAULT_PATTERN)
    assert got == expected
    for lab_id in ("90234654", "0234769", "90234769", "06350320", "06350116"):
        assert got != lab_id


def test_ocr_v_misread_of_the_slash_still_matches():
    """"Hospital/Clinic" OCRs as "HospitavClinic"; the label must still match."""
    for spelling in ("Hospital/Clinic", "HospitalClinic", "HospitavClinic",
                     "HospitaVClinic", "Hospital / Clinic"):
        text = f"HN {spelling} : 8258"
        assert find_opd_regex(text, DEFAULT_PATTERN) == "8258", spelling


def test_binarized_split_relies_on_opd_fallback():
    """Documents a known gap: when binarization strands the value on its own
    line, the field-anchored branch cannot reach it. Lab2 survives only
    because it happens to carry an "OPD " prefix. The same OCR damage on a
    report without that prefix yields None -- i.e. the unfiled queue, which
    is the safe failure, but it is a miss and not a match."""
    split_with_prefix = (
        "HN VET: 0234769 HN HospitavClinic\n"
        "LN VET: 06350320 Owner Name: -\n"
        ": OPD 9654"
    )
    split_without_prefix = split_with_prefix.replace(": OPD 9654", ": 9654")

    assert find_opd_regex(split_with_prefix, ANCHORED_ONLY) is None
    assert find_opd_regex(split_with_prefix, DEFAULT_PATTERN) == "9654"
    assert find_opd_regex(split_without_prefix, DEFAULT_PATTERN) is None
