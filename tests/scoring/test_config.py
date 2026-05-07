"""Phase 0 acceptance: weights sum to 1.0, defaults match spec section 4."""

from __future__ import annotations

import pytest

from scanner.scoring.config import (
    DEFAULT_TOD_MULTIPLIERS,
    GateThresholds,
    ScoringWeights,
    TierThresholds,
)


def test_default_weights_sum_to_one() -> None:
    w = ScoringWeights()
    total = sum(w.as_dict().values())
    assert abs(total - 1.0) < 1e-9


def test_default_weight_values_match_spec() -> None:
    w = ScoringWeights()
    assert w.rvol_30m == pytest.approx(0.15)
    assert w.momentum_atr == pytest.approx(0.15)
    assert w.rvol_cumulative == pytest.approx(0.10)
    assert w.vwap_distance_atr == pytest.approx(0.10)
    assert w.trend_stack_5m == pytest.approx(0.10)
    assert w.mtf_alignment == pytest.approx(0.10)
    assert w.rsi_intraday == pytest.approx(0.10)
    assert w.breakout_proximity == pytest.approx(0.10)
    assert w.clean_structure == pytest.approx(0.10)


def test_weights_must_sum_to_one_strictly() -> None:
    with pytest.raises(ValueError, match="must sum to 1.0"):
        ScoringWeights(rvol_30m=0.2)


def test_gate_thresholds_match_spec() -> None:
    g = GateThresholds()
    assert g.min_price == 5.0
    assert g.max_price == 2000.0  # widened from spec's 500 to admit higher-priced names
    assert g.min_adv_dollar == 20_000_000.0
    assert g.min_today_cum_dollar_volume == 3_000_000.0
    assert g.max_avg_spread_pct == 0.10
    assert g.min_atr_pct == 2.0


def test_tier_thresholds_match_spec() -> None:
    t = TierThresholds()
    assert t.a == 85.0
    assert t.b == 75.0
    assert t.c == 70.0


def test_tod_table_has_six_default_windows() -> None:
    assert len(DEFAULT_TOD_MULTIPLIERS.windows) == 6
    multipliers = [w.multiplier for w in DEFAULT_TOD_MULTIPLIERS.windows]
    assert multipliers == [0.85, 1.20, 1.10, 0.85, 1.10, 0.95]
