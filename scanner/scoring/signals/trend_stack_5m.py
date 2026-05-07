"""trend_stack_5m — 4-of-4 trend stack on 5m bars.

Checks: close > VWAP, close > EMA9, EMA9 > EMA20, EMA20 > EMA50.
Strength is `count / 4`, raw is the integer count.
"""

from __future__ import annotations

from ..context import SignalContext
from ..indicators import trend_stack_count
from ..types import SignalResult

NAME = "trend_stack_5m"


def compute(ctx: SignalContext) -> SignalResult:
    bars_5m = ctx.bars.get("5m") or []
    result = trend_stack_count(bars_5m, ctx.session_start_ms)
    if result is None:
        return SignalResult(NAME, 0.0, None)
    strength, count = result
    return SignalResult(NAME, strength, count)
