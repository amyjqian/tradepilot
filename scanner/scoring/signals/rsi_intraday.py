"""rsi_intraday — RSI(14) on 5m closes, mapped to a piecewise curve.

Curve:
  < 50              → 0
  50 → 60           → ramp 0 → 1
  60 → 80           → 1.0 (sweet spot)
  80 → 90           → decay 1 → 0.3
  > 90              → 0
"""

from __future__ import annotations

from ..context import SignalContext
from ..indicators import rsi
from ..types import SignalResult

NAME = "rsi_intraday"


def compute(ctx: SignalContext) -> SignalResult:
    bars_5m = ctx.bars.get("5m") or []
    if len(bars_5m) < 15:
        return SignalResult(NAME, 0.0, None)
    closes = [b.close for b in bars_5m]
    value = rsi(closes, 14)[-1]
    if value is None:
        return SignalResult(NAME, 0.0, None)
    if value < 50.0 or value > 90.0:
        strength = 0.0
    elif value <= 60.0:
        strength = (value - 50.0) / 10.0
    elif value <= 80.0:
        strength = 1.0
    else:
        strength = 1.0 - 0.7 * (value - 80.0) / 10.0
    return SignalResult(NAME, strength, value)
