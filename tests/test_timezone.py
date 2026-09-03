"""Timezone boundary: an event timestamp late in the UTC day must file
under the *next* Bangkok (UTC+7) date, not server local time / now().
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.pipeline import received_date


def _epoch_ms(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> int:
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def test_2330_utc_files_under_next_bangkok_date():
    # 2026-09-03 23:30 UTC == 2026-09-04 06:30 Asia/Bangkok (UTC+7)
    ts_ms = _epoch_ms(2026, 9, 3, 23, 30)
    assert received_date(ts_ms, "Asia/Bangkok") == "2026-09-04"


def test_just_before_bangkok_rollover_stays_same_day():
    # 2026-09-03 16:59 UTC == 2026-09-03 23:59 Asia/Bangkok
    ts_ms = _epoch_ms(2026, 9, 3, 16, 59)
    assert received_date(ts_ms, "Asia/Bangkok") == "2026-09-03"


def test_just_after_bangkok_rollover_advances_a_day():
    # 2026-09-03 17:00 UTC == 2026-09-04 00:00 Asia/Bangkok
    ts_ms = _epoch_ms(2026, 9, 3, 17, 0)
    assert received_date(ts_ms, "Asia/Bangkok") == "2026-09-04"


def test_early_utc_is_same_bangkok_date():
    # 2026-09-03 00:00 UTC == 2026-09-03 07:00 Asia/Bangkok
    ts_ms = _epoch_ms(2026, 9, 3, 0, 0)
    assert received_date(ts_ms, "Asia/Bangkok") == "2026-09-03"


def test_different_timezone_is_respected():
    # Sanity check that the function isn't hardcoded to Bangkok.
    ts_ms = _epoch_ms(2026, 9, 3, 23, 30)
    assert received_date(ts_ms, "UTC") == "2026-09-03"
