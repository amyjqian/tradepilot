"""15m bias: long / short / neutral.

Long requires `ema9 > ema21` AND `close > vwap_15m`. Short is the mirror
(`ema9 < ema21` AND `close < vwap_15m`). Anything else is neutral.
"""

from __future__ import annotations

from .context import SignalContext
from .indicators import ema, vwap_session
from .types import Bias


def bias_15m(ctx: SignalContext) -> Bias:
    bars_15m = ctx.bars.get("15m") or []
    if len(bars_15m) < 21:
        return "neutral"
    closes = [b.close for b in bars_15m]
    e9 = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    vw = vwap_session(bars_15m, ctx.session_start_ms)[-1]
    close = closes[-1]
    if e9 is None or e21 is None or vw is None:
        return "neutral"
    if e9 > e21 and close > vw:
        return "long"
    if e9 < e21 and close < vw:
        return "short"
    return "neutral"
