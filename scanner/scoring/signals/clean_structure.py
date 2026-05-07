"""clean_structure — count direction flips in last 20 5m closes.

A flip at index i is `(c[i] > c[i-1]) != (c[i-1] > c[i-2])`. Strength is
`1 − flips / 20`, clipped at 0.
"""

from __future__ import annotations

from ..context import SignalContext
from ..types import SignalResult

NAME = "clean_structure"


def compute(ctx: SignalContext) -> SignalResult:
    bars_5m = ctx.bars.get("5m") or []
    if len(bars_5m) < 20:
        return SignalResult(NAME, 0.0, None)
    closes = [b.close for b in bars_5m[-20:]]
    flips = 0
    for i in range(2, len(closes)):
        up_now = closes[i] > closes[i - 1]
        up_prev = closes[i - 1] > closes[i - 2]
        if up_now != up_prev:
            flips += 1
    strength = max(0.0, 1.0 - flips / 20.0)
    return SignalResult(NAME, strength, flips)
