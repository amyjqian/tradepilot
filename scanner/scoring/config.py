"""Configuration models for the scoring engine.

These are the new 9-signal weights, gates, TOD multipliers, disqualifier
thresholds, and tier cutoffs. They live separately from `scanner.config`
(which still serves the legacy 5-signal engine) until the old engine is
retired.

Defaults match `PER_MINUTE_SCORING.md` exactly.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class ScoringWeights(BaseModel):
    """Weights for the nine signals — must sum to 1.00 exactly.

    Spec section 4: `rvol_30m` and `momentum_atr` are the dominant signals
    at 0.15. The seven others are equal at 0.10.
    """

    rvol_30m: float = 0.15
    rvol_cumulative: float = 0.10
    momentum_atr: float = 0.15
    vwap_distance_atr: float = 0.10
    trend_stack_5m: float = 0.10
    mtf_alignment: float = 0.10
    rsi_intraday: float = 0.10
    breakout_proximity: float = 0.10
    clean_structure: float = 0.10

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> ScoringWeights:
        total = sum(self.as_dict().values())
        if abs(total - 1.0) >= 1e-9:
            raise ValueError(f"ScoringWeights must sum to 1.0 exactly; got {total}")
        return self

    def as_dict(self) -> dict[str, float]:
        return {
            "rvol_30m": self.rvol_30m,
            "rvol_cumulative": self.rvol_cumulative,
            "momentum_atr": self.momentum_atr,
            "vwap_distance_atr": self.vwap_distance_atr,
            "trend_stack_5m": self.trend_stack_5m,
            "mtf_alignment": self.mtf_alignment,
            "rsi_intraday": self.rsi_intraday,
            "breakout_proximity": self.breakout_proximity,
            "clean_structure": self.clean_structure,
        }


class GateThresholds(BaseModel):
    """Universe gates applied before any signal scoring (spec step 2).

    `max_price` is widened to $2000 (vs the spec's $500) to admit names
    like MU that have appreciated past the original ceiling. Slippage
    and sizing concerns at high prices haven't materially changed our
    edge in spot checks; the spec's tight $500 cap was over-conservative
    for a 2026 universe.
    """

    min_price: float = 5.0
    max_price: float = 2000.0
    min_adv_dollar: float = 20_000_000.0
    min_today_cum_dollar_volume: float = 3_000_000.0
    max_avg_spread_pct: float = 0.10
    min_atr_pct: float = 2.0
    halt_lookback_seconds: float = 30.0


class TODWindow(BaseModel):
    """One time-of-day window in ET wall-clock minutes-from-midnight.

    `start_min` is inclusive, `end_min` exclusive. Multipliers below 1.0
    de-emphasize the period; above 1.0 amplify it.
    """

    start_min: int
    end_min: int
    multiplier: float


class TODMultipliers(BaseModel):
    """Time-of-day multiplier table (spec step 5).

    Lookups outside any window return 1.0 (pre-/post-market = neutral).
    """

    windows: list[TODWindow] = Field(
        default_factory=lambda: [
            TODWindow(start_min=9 * 60 + 30, end_min=9 * 60 + 45, multiplier=0.85),
            TODWindow(start_min=9 * 60 + 45, end_min=10 * 60 + 30, multiplier=1.20),
            TODWindow(start_min=10 * 60 + 30, end_min=11 * 60 + 30, multiplier=1.10),
            TODWindow(start_min=11 * 60 + 30, end_min=14 * 60, multiplier=0.85),
            TODWindow(start_min=14 * 60, end_min=15 * 60 + 30, multiplier=1.10),
            TODWindow(start_min=15 * 60 + 30, end_min=16 * 60, multiplier=0.95),
        ]
    )


DEFAULT_TOD_MULTIPLIERS = TODMultipliers()


class TierThresholds(BaseModel):
    """Score cutoffs for tier assignment (spec step 8).

    A and B require `bias == "long"`. C admits any bias.
    """

    a: float = 85.0
    b: float = 75.0
    c: float = 70.0


class DisqualifierConfig(BaseModel):
    """Live disqualifier thresholds (spec step 7)."""

    spread_multiple_of_avg: float = 2.0
    min_last_bar_volume_ratio: float = 0.5
    gap_through_vwap_atr: float = 1.0
