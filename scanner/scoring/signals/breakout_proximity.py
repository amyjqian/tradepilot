"""breakout_proximity — distance below `min(prior_today_high, yesterday_high)`.

The signal goes positive when price is approaching (but hasn't broken
through) the most-recent established resistance, with the last 5 1m bars
showing an upward push.

Two refinements vs the spec defaults, applied here:

1. **`target` excludes the latest 1m bar's contribution.** Using the
   global `today_high` (which is updated as each 1m bar closes) lets a
   stock that just printed a new HoD on the latest bar score positive
   against its own intra-bar wick — that's "broken through," not
   "approaching." Computing `prior_today_high = max(b.high for b in
   bars_1m[:-1])` makes the signal robust to that case.

2. **Higher-highs check tolerates one violation.** The spec's strict
   "all 5 highs strictly ascending" is too tight in practice — a real
   breakout is often "rising into the level with one minor pullback."
   Predicate is now `>=` (flat OK) and at least 3 of the 4 transitions
   between adjacent highs must satisfy it (1 down move tolerated).
"""

from __future__ import annotations

from ..context import SignalContext
from ..indicators import clip
from ..types import SignalResult

NAME = "breakout_proximity"


def compute(ctx: SignalContext) -> SignalResult:
    bars_1m = ctx.bars.get("1m") or []
    if len(bars_1m) < 5:
        return SignalResult(NAME, 0.0, None)

    # Target = the lower of yesterday's high and today's HoD established
    # by bars BEFORE the most recent one. Excluding the latest bar keeps
    # the signal honest when that bar IS the new HoD.
    prior_highs = [b.high for b in bars_1m[:-1]]
    if not prior_highs:
        return SignalResult(NAME, 0.0, None)
    prior_today_high = max(prior_highs)
    target = min(prior_today_high, ctx.yesterday_high)
    if target <= 0:
        return SignalResult(NAME, 0.0, None)

    close = bars_1m[-1].close
    distance_pct = (target - close) / close * 100.0

    # 4 transitions across the last 5 highs; tolerate 1 down move (3 of
    # 4 must be non-decreasing).
    last5_highs = [b.high for b in bars_1m[-5:]]
    ascending_count = sum(
        1 for i in range(1, 5) if last5_highs[i] >= last5_highs[i - 1]
    )
    has_higher_highs = ascending_count >= 3

    if not has_higher_highs or distance_pct < 0:
        return SignalResult(NAME, 0.0, distance_pct)
    strength = clip(1.0 - distance_pct / 5.0, 0.0, 1.0)
    return SignalResult(NAME, strength, distance_pct)
