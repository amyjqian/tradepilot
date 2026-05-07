"""momentum_atr — today's % move from prior close, normalized by daily ATR%.

Reads: last 1m close as "now's price"; daily bars to compute 14-period ATR%.
Mapping: linear, 0 ATR → 0, 2 ATR → 1, negative clipped to 0.
"""

from __future__ import annotations

from ..context import SignalContext
from ..indicators import atr_pct, clip
from ..types import SignalResult

NAME = "momentum_atr"


def compute(ctx: SignalContext) -> SignalResult:
    bars_1m = ctx.bars.get("1m") or []
    bars_d = ctx.bars.get("daily") or []
    if not bars_1m or len(bars_d) < 15 or ctx.prior_session_close <= 0:
        return SignalResult(NAME, 0.0, None)
    pct_move = (bars_1m[-1].close - ctx.prior_session_close) / ctx.prior_session_close * 100.0
    atr_pct_value = atr_pct(bars_d, 14)[-1]
    if atr_pct_value is None or atr_pct_value <= 0:
        return SignalResult(NAME, 0.0, None)
    move_in_atr = pct_move / atr_pct_value
    strength = clip(move_in_atr / 2.0, 0.0, 1.0)
    return SignalResult(NAME, strength, move_in_atr)
