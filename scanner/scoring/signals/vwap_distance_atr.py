"""vwap_distance_atr — (close − VWAP) / 5m ATR.

Triangular curve: 0 → 0, +0.5 ATR → 1, +2.0 ATR → 0. Negative or beyond
+2.0 ATR clamps to 0.
"""

from __future__ import annotations

from ..context import SignalContext
from ..indicators import atr_value, vwap_session
from ..types import SignalResult

NAME = "vwap_distance_atr"


def compute(ctx: SignalContext) -> SignalResult:
    bars_5m = ctx.bars.get("5m") or []
    if len(bars_5m) < 15:
        return SignalResult(NAME, 0.0, None)
    vwap = vwap_session(bars_5m, ctx.session_start_ms)[-1]
    atr5 = atr_value(bars_5m, 14)[-1]
    if vwap is None or atr5 is None or atr5 <= 0:
        return SignalResult(NAME, 0.0, None)
    distance_atr = (bars_5m[-1].close - vwap) / atr5
    if distance_atr <= 0.0:
        strength = 0.0
    elif distance_atr <= 0.5:
        strength = distance_atr / 0.5
    elif distance_atr <= 2.0:
        strength = (2.0 - distance_atr) / 1.5
    else:
        strength = 0.0
    return SignalResult(NAME, strength, distance_atr)
