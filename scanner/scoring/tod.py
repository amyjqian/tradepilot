"""Time-of-day multiplier (spec step 5).

The TOD table is wall-clock ET. Bar timestamps are epoch ms (UTC). Convert
through `zoneinfo.ZoneInfo("America/New_York")` so DST is correct on the
2026-03-08 (spring) and 2026-11-01 (fall) transition days.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import TODMultipliers

ET = ZoneInfo("America/New_York")


def et_minute_of_day(now_ms: int) -> int:
    """Return ET wall-clock minutes-from-midnight for the given epoch ms."""
    dt = datetime.fromtimestamp(now_ms / 1000.0, tz=ET)
    return dt.hour * 60 + dt.minute


def tod_multiplier(now_ms: int, table: TODMultipliers) -> float:
    minute = et_minute_of_day(now_ms)
    for window in table.windows:
        if window.start_min <= minute < window.end_min:
            return window.multiplier
    return 1.0
