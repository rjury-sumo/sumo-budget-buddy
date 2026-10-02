"""timerange.py — resolve a scope's configured time window to absolute UTC
epoch-ms bounds, independent of whatever index/query will be run against it.

Supported `window` values:
  "today"        - midnight (local `tz`) through now, today
  "yesterday"    - full previous calendar day (local `tz`)
  "last_Nh"      - rolling N-hour window ending now, e.g. "last_6h"
  "YYYY-MM-DD/YYYY-MM-DD" - explicit local-date range, inclusive start,
                   exclusive end (second date's midnight)

All resolution happens client-side with stdlib `zoneinfo` — the Search Job
API itself only ever sees absolute epoch-ms bounds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_LAST_N_HOURS_RE = re.compile(r"^last_(\d+)h$")
_DATE_RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})$")


class TimeRangeError(ValueError):
    """Raised for an unparseable `window` value or unknown timezone."""


@dataclass(frozen=True)
class ResolvedWindow:
    from_ms: int
    to_ms: int
    tz: str
    window: str
    from_local: datetime
    to_local: datetime


def _tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001 - surface as our own error type
        raise TimeRangeError(f"unknown timezone {tz_name!r}") from exc


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def resolve_window(window: str, tz_name: str, *, now: datetime | None = None) -> ResolvedWindow:
    """Resolve `window` (as configured on a scope) to UTC epoch-ms bounds.

    `now` is injectable for tests; defaults to the current time in `tz_name`.
    """
    tz = _tz(tz_name)
    now_local = now.astimezone(tz) if now else datetime.now(tz)

    w = window.strip().lower()

    if w == "today":
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now_local
        return ResolvedWindow(_ms(start), _ms(end), tz_name, window, start, end)

    if w == "yesterday":
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = today_start - timedelta(days=1)
        end = today_start
        return ResolvedWindow(_ms(start), _ms(end), tz_name, window, start, end)

    m = _LAST_N_HOURS_RE.match(w)
    if m:
        hours = int(m.group(1))
        end = now_local
        start = end - timedelta(hours=hours)
        return ResolvedWindow(_ms(start), _ms(end), tz_name, window, start, end)

    m = _DATE_RANGE_RE.match(window.strip())
    if m:
        start = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=tz)
        end = datetime.strptime(m.group(2), "%Y-%m-%d").replace(tzinfo=tz)
        if end <= start:
            raise TimeRangeError(f"explicit window end must be after start: {window!r}")
        return ResolvedWindow(_ms(start), _ms(end), tz_name, window, start, end)

    raise TimeRangeError(
        f"unrecognized window {window!r} — expected 'today', 'yesterday', "
        "'last_Nh', or 'YYYY-MM-DD/YYYY-MM-DD'"
    )


def end_of_day(tz_name: str, *, on: datetime | None = None) -> datetime:
    """23:59:59.999999 in `tz_name` on the given day (default: today)."""
    tz = _tz(tz_name)
    ref = (on.astimezone(tz) if on else datetime.now(tz))
    return ref.replace(hour=23, minute=59, second=59, microsecond=999999)
