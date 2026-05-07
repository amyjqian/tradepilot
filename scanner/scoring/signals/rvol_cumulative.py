"""rvol_cumulative — today's cumulative $-volume vs typical at this TOD.

Mapping: linear, 1.0× → 0, 3.0× → 1, clipped to [0, 1].
"""

from __future__ import annotations

from ..context import SignalContext
from ..indicators import clip
from ..types import SignalResult

NAME = "rvol_cumulative"


def compute(ctx: SignalContext) -> SignalResult:
    if ctx.historical_cum_dollar_volume_at_tod <= 0:
        return SignalResult(NAME, 0.0, None)
    ratio = ctx.today_cum_dollar_volume / ctx.historical_cum_dollar_volume_at_tod
    strength = clip((ratio - 1.0) / (3.0 - 1.0), 0.0, 1.0)
    return SignalResult(NAME, strength, ratio)
