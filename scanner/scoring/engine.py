"""`score_symbol` — the 8-step scoring orchestrator.

Async signature matches the spec; the body is pure CPU. The runner can use
`asyncio.to_thread(score_symbol_sync, ctx)` if it wants real parallelism
across symbols. Within one call, signals run sequentially.
"""

from __future__ import annotations

from .bias import bias_15m
from .composite import composite_score
from .config import (
    DEFAULT_TOD_MULTIPLIERS,
    DisqualifierConfig,
    GateThresholds,
    ScoringWeights,
    TierThresholds,
    TODMultipliers,
)
from .context import SignalContext
from .disqualifiers import is_disqualified
from .gates import passes_gates
from .signals import (
    breakout_proximity,
    clean_structure,
    momentum_atr,
    mtf_alignment,
    rsi_intraday,
    rvol_30m,
    rvol_cumulative,
    trend_stack_5m,
    vwap_distance_atr,
)
from .tier import assign_tier
from .tod import tod_multiplier
from .types import ScoreResult, SignalResult


def score_symbol_sync(
    ctx: SignalContext,
    weights: ScoringWeights | None = None,
    gates: GateThresholds | None = None,
    tod: TODMultipliers | None = None,
    tier_thresholds: TierThresholds | None = None,
    dq_config: DisqualifierConfig | None = None,
) -> ScoreResult | None:
    """Synchronous core. Returns the `ScoreResult` or `None` if the symbol
    failed gates / disqualifiers. Use `diagnose_symbol_sync` when you also
    need to know *why* a symbol was rejected.
    """
    result, _ = diagnose_symbol_sync(ctx, weights, gates, tod, tier_thresholds, dq_config)
    return result


def diagnose_symbol_sync(
    ctx: SignalContext,
    weights: ScoringWeights | None = None,
    gates: GateThresholds | None = None,
    tod: TODMultipliers | None = None,
    tier_thresholds: TierThresholds | None = None,
    dq_config: DisqualifierConfig | None = None,
) -> tuple[ScoreResult | None, list[str]]:
    """Like `score_symbol_sync` but also returns rejection reasons.

    The reasons list is empty on a successful score. On rejection, each
    entry is prefixed with `gate:` (universe filter failed) or `dq:`
    (live disqualifier fired).
    """
    weights = weights or ScoringWeights()
    gates = gates or GateThresholds()
    tod = tod or DEFAULT_TOD_MULTIPLIERS
    tier_thresholds = tier_thresholds or TierThresholds()
    dq_config = dq_config or DisqualifierConfig()

    passed, gate_reasons = passes_gates(ctx, gates)
    if not passed:
        return None, [f"gate:{r}" for r in gate_reasons]

    components: dict[str, SignalResult] = {
        "rvol_30m": rvol_30m.compute(ctx),
        "rvol_cumulative": rvol_cumulative.compute(ctx),
        "momentum_atr": momentum_atr.compute(ctx),
        "vwap_distance_atr": vwap_distance_atr.compute(ctx),
        "trend_stack_5m": trend_stack_5m.compute(ctx),
        "mtf_alignment": mtf_alignment.compute(ctx),
        "rsi_intraday": rsi_intraday.compute(ctx),
        "breakout_proximity": breakout_proximity.compute(ctx),
        "clean_structure": clean_structure.compute(ctx),
    }

    base_score = composite_score(components, weights)
    mult = tod_multiplier(ctx.now_ms, tod)
    final_score = base_score * mult

    bias = bias_15m(ctx)

    dq, flags = is_disqualified(ctx, dq_config)
    if dq:
        return None, [f"dq:{f}" for f in flags]

    tier = assign_tier(final_score, bias, tier_thresholds)

    return (
        ScoreResult(
            symbol=ctx.symbol,
            timestamp=ctx.now_ms,
            base_score=base_score,
            tod_mult=mult,
            final_score=final_score,
            bias_15m=bias,
            flags=flags,
            tier=tier,
            components=components,
        ),
        [],
    )


async def score_symbol(
    ctx: SignalContext,
    weights: ScoringWeights | None = None,
    gates: GateThresholds | None = None,
    tod: TODMultipliers | None = None,
    tier_thresholds: TierThresholds | None = None,
    dq_config: DisqualifierConfig | None = None,
) -> ScoreResult | None:
    return score_symbol_sync(ctx, weights, gates, tod, tier_thresholds, dq_config)


# Re-export for convenient callers.
__all__ = ["diagnose_symbol_sync", "score_symbol", "score_symbol_sync"]
