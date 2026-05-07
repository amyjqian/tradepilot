"""Universe gates — the spec's step 2.

`passes_gates` returns `(True, [])` on pass and `(False, reasons)` on
fail. The reasons list is human-readable and short — a UI can render it
inline next to the symbol.
"""

from __future__ import annotations

from .config import GateThresholds
from .context import SignalContext


def passes_gates(ctx: SignalContext, gates: GateThresholds) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    bars_1m = ctx.bars.get("1m") or []
    if not bars_1m:
        return False, ["no_1m_bars"]
    last_close = bars_1m[-1].close

    if last_close < gates.min_price or last_close > gates.max_price:
        reasons.append(f"price {last_close} outside [{gates.min_price}, {gates.max_price}]")

    if ctx.adv_dollar < gates.min_adv_dollar:
        reasons.append(f"adv_dollar {ctx.adv_dollar:.0f} < {gates.min_adv_dollar:.0f}")

    if ctx.today_cum_dollar_volume < gates.min_today_cum_dollar_volume:
        reasons.append(
            f"today_cum_dollar_volume {ctx.today_cum_dollar_volume:.0f} < "
            f"{gates.min_today_cum_dollar_volume:.0f}"
        )

    if ctx.avg_spread_pct > gates.max_avg_spread_pct:
        reasons.append(
            f"avg_spread_pct {ctx.avg_spread_pct:.3f} > {gates.max_avg_spread_pct:.3f}"
        )

    if ctx.atr_pct_20d < gates.min_atr_pct:
        reasons.append(f"atr_pct_20d {ctx.atr_pct_20d:.2f} < {gates.min_atr_pct:.2f}")

    if ctx.halted_recently:
        reasons.append("halted_recently")

    return (len(reasons) == 0), reasons
