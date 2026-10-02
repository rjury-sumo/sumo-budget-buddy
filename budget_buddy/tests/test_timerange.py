#!/usr/bin/env python3
"""
test_timerange.py — Unit tests for budget_buddy/timerange.py.

No credentials, no network. Covers window resolution (today/yesterday/
last_Nh/explicit date range) and end_of_day, all timezone-aware via injected
`now`.

Run:
    uv run pytest budget_buddy/tests/test_timerange.py
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from budget_buddy.timerange import TimeRangeError, end_of_day, resolve_window

PT = ZoneInfo("America/Los_Angeles")
NOON_PT = datetime(2026, 6, 15, 12, 0, 0, tzinfo=PT)


def test_today_window_starts_at_local_midnight():
    w = resolve_window("today", "America/Los_Angeles", now=NOON_PT)
    assert w.from_local.hour == 0 and w.from_local.minute == 0
    assert w.to_local == NOON_PT
    assert w.from_ms < w.to_ms


def test_yesterday_window_is_full_previous_day():
    w = resolve_window("yesterday", "America/Los_Angeles", now=NOON_PT)
    assert w.from_local.day == 14
    assert w.to_local.day == 15
    assert w.to_local.hour == 0
    assert (w.to_ms - w.from_ms) == 24 * 3600 * 1000


def test_last_n_hours_window():
    w = resolve_window("last_6h", "America/Los_Angeles", now=NOON_PT)
    assert (w.to_ms - w.from_ms) == 6 * 3600 * 1000
    assert w.to_local == NOON_PT


def test_explicit_date_range():
    w = resolve_window("2026-06-01/2026-06-03", "America/Los_Angeles", now=NOON_PT)
    assert w.from_local.day == 1
    assert w.to_local.day == 3
    assert (w.to_ms - w.from_ms) == 2 * 24 * 3600 * 1000


def test_explicit_date_range_end_before_start_rejected():
    with pytest.raises(TimeRangeError):
        resolve_window("2026-06-03/2026-06-01", "America/Los_Angeles", now=NOON_PT)


def test_unrecognized_window_rejected():
    with pytest.raises(TimeRangeError):
        resolve_window("last_week", "America/Los_Angeles", now=NOON_PT)


def test_unknown_timezone_rejected():
    with pytest.raises(TimeRangeError):
        resolve_window("today", "Not/ARealZone", now=NOON_PT)


def test_window_is_case_insensitive():
    a = resolve_window("TODAY", "America/Los_Angeles", now=NOON_PT)
    b = resolve_window("today", "America/Los_Angeles", now=NOON_PT)
    assert a.from_ms == b.from_ms and a.to_ms == b.to_ms


def test_end_of_day_is_last_microsecond_of_the_local_day():
    eod = end_of_day("America/Los_Angeles", on=NOON_PT)
    assert (eod.hour, eod.minute, eod.second) == (23, 59, 59)
    assert eod.day == NOON_PT.day


def test_end_of_day_respects_timezone_not_utc():
    # Same instant, two different TZs -> different local "end of day" dates
    # possible near the boundary; here just confirm the tz is honored.
    eod_pt = end_of_day("America/Los_Angeles", on=NOON_PT)
    eod_utc = end_of_day("UTC", on=NOON_PT)
    assert eod_pt.tzinfo.key == "America/Los_Angeles"
    assert str(eod_utc.tzinfo) == "UTC"
