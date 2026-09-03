"""Filename sequencing: .jpg -> _2.jpg -> _3.jpg, with the photo and its
.md transcript always sharing the same stem.
"""

from __future__ import annotations

from app.onedrive import candidate_stem


def test_first_result_has_no_suffix():
    assert candidate_stem("2026-09-03", 1) == "2026-09-03"


def test_second_result_gets_suffix():
    assert candidate_stem("2026-09-03", 2) == "2026-09-03_2"


def test_third_result_gets_suffix():
    assert candidate_stem("2026-09-03", 3) == "2026-09-03_3"


def test_sequence_progression():
    stems = [candidate_stem("2026-09-03", seq) for seq in (1, 2, 3, 4)]
    assert stems == ["2026-09-03", "2026-09-03_2", "2026-09-03_3", "2026-09-03_4"]


def test_photo_and_md_share_the_same_stem():
    for seq in (1, 2, 3):
        stem = candidate_stem("2026-09-03", seq)
        jpg_name = f"{stem}.jpg"
        md_name = f"{stem}.md"
        assert jpg_name.rsplit(".", 1)[0] == md_name.rsplit(".", 1)[0] == stem
