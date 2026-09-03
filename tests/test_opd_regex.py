"""OPD regex: positive matches (Thai and English variants) and strings that
must NOT match, against the default OPD_REGEX from app.config.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.extract import find_opd_regex, normalize_opd

DEFAULT_PATTERN = Settings.model_fields["opd_regex"].default

POSITIVE = [
    ("OPD 12345", "12345"),
    ("OPD No. 12345", "12345"),
    ("OPD No: 64-001234", "64001234"),
    ("O.P.D. 998877", "998877"),
    ("HN 445566", "445566"),
    ("HN: 100-200-3", "1002003"),
    ("OPD เลขที่ 778899", "778899"),
    ("OPD:12345", "12345"),
    ("opd number 12345", "12345"),
    ("Patient info\nOPD No. 000123\nName: Somchai", "000123"),
]

NEGATIVE = [
    "no identifiers here",
    "Patient age: 45",
    "Report date: 2026-09-03",
    "Temperature 98.6",
    "OPD services available",
]


@pytest.mark.parametrize("text,expected", POSITIVE)
def test_positive_matches(text, expected):
    assert find_opd_regex(text, DEFAULT_PATTERN) == expected


@pytest.mark.parametrize("text", NEGATIVE)
def test_negative_matches(text):
    assert find_opd_regex(text, DEFAULT_PATTERN) is None


def test_normalize_strips_dashes_and_slashes():
    assert normalize_opd("64-00/1234") == "64001234"


def test_normalize_leaves_plain_digits_untouched():
    assert normalize_opd("445566") == "445566"
