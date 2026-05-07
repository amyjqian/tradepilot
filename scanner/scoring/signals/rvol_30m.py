"""rvol_30m — recent 30-min $-volume vs typical at this TOD.

Reads: 1m bars (last 30) and `historical_30min_dollar_volume`.
Mapping: linear, 1.5× → 0, 4.0× → 1, clipped to [0, 1].
"""

from __future__ import annotations

from ..context import SignalContext
from ..indicators import clip
from ..types import SignalResult

NAME = "rvol_30m"


def compute(ctx: SignalContext) -> SignalResult:
    bars_1m = ctx.bars.get("1m") or []
    if len(bars_1m) < 30 or ctx.historical_30min_dollar_volume <= 0:
        return SignalResult(NAME, 0.0, None)
    recent = bars_1m[-30:]
    recent_dollar_vol = sum(b.close * b.volume for b in recent)
    ratio = recent_dollar_vol / ctx.historical_30min_dollar_volume
    strength = clip((ratio - 1.5) / (4.0 - 1.5), 0.0, 1.0)
    return SignalResult(NAME, strength, ratio)
