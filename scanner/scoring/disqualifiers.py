"""Live disqualifiers (spec step 7).

A disqualified symbol returns `None` from `score_symbol` regardless of
score. Each check returns a short flag string explaining the reason; the
full list of fired flags is returned alongside the boolean.
"""

from __future__ import annotations

from .config import DisqualifierConfig
from .context import SignalContext
from .indicators import atr_value, vwap_session


def is_disqualified(
    ctx: SignalContext, cfg: DisqualifierConfig
) -> tuple[bool, list[str]]:
    flags: list[str] = []

    if ctx.halted_recently:
        flags.append("halted_recently")

    if ctx.rolling_avg_spread_pct > 0 and ctx.avg_spread_pct > (
        cfg.spread_multiple_of_avg * ctx.rolling_avg_spread_pct
    ):
        flags.append("spread_blowout")

    if ctx.avg_1m_volume_20bar > 0:
        ratio = ctx.last_1m_volume / ctx.avg_1m_volume_20bar
        if ratio < cfg.min_last_bar_volume_ratio:
            flags.append("volume_dry_up")

    if _gap_through_vwap_atr(ctx) >= cfg.gap_through_vwap_atr:
        flags.append("gap_through_vwap")

    return (len(flags) > 0), flags


def _gap_through_vwap_atr(ctx: SignalContext) -> float:
    """How many 5m ATRs is the current close *below* VWAP? Negative if above."""
    bars_5m = ctx.bars.get("5m") or []
    if len(bars_5m) < 15:
        return 0.0
    vw = vwap_session(bars_5m, ctx.session_start_ms)[-1]
    atr5 = atr_value(bars_5m, 14)[-1]
    if vw is None or atr5 is None or atr5 <= 0:
        return 0.0
    close = bars_5m[-1].close
    return (vw - close) / atr5
