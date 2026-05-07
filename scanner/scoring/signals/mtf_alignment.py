"""mtf_alignment — count of timeframes whose trend stack ≥ 0.75.

Reads 1m, 5m, 15m bars. Strength is `qualifying / 3`. Each TF that lacks
enough bars to compute a stack counts as not-qualifying (does not error).
"""

from __future__ import annotations

from ..context import SignalContext
from ..indicators import trend_stack_count
from ..types import SignalResult

NAME = "mtf_alignment"
QUALIFYING_THRESHOLD = 0.75


def compute(ctx: SignalContext) -> SignalResult:
    qualifying = 0
    for tf in ("1m", "5m", "15m"):
        bars = ctx.bars.get(tf) or []
        result = trend_stack_count(bars, ctx.session_start_ms)
        if result is None:
            continue
        strength, _ = result
        if strength >= QUALIFYING_THRESHOLD:
            qualifying += 1
    return SignalResult(NAME, qualifying / 3.0, qualifying)
