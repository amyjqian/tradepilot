"""Spec section 6 worked-example arithmetic — composite, TOD, tier, gates.

These tests stub out the nine signal strengths with the exact values from
section 6.3 and assert that the rest of the pipeline (composite, TOD, tier
assignment) reproduces section 6.4–6.8 to within 1e-3.

The per-signal math (curve shapes, mappings) is verified separately in
`test_signals.py` against minimal hand-crafted contexts. Splitting the two
keeps this test free of indicator-fixture noise.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scanner.scoring.composite import composite_score
from scanner.scoring.config import (
    DEFAULT_TOD_MULTIPLIERS,
    GateThresholds,
    ScoringWeights,
    TierThresholds,
)
from scanner.scoring.gates import passes_gates
from scanner.scoring.tier import assign_tier
from scanner.scoring.tod import tod_multiplier
from scanner.scoring.types import SignalResult

ET = ZoneInfo("America/New_York")

# Section 6.3 — exact strengths from the worked example.
NVDA_SIGNALS_AT_10_42 = {
    "rvol_30m": SignalResult("rvol_30m", 0.656, 3.141),
    "rvol_cumulative": SignalResult("rvol_cumulative", 0.731, 2.462),
    "momentum_atr": SignalResult("momentum_atr", 0.429, 0.858),
    "vwap_distance_atr": SignalResult("vwap_distance_atr", 0.619, 1.071),
    "trend_stack_5m": SignalResult("trend_stack_5m", 1.000, 4),
    "mtf_alignment": SignalResult("mtf_alignment", 1.000, 3),
    "rsi_intraday": SignalResult("rsi_intraday", 1.000, 67.2),
    "breakout_proximity": SignalResult("breakout_proximity", 0.968, 0.161),
    "clean_structure": SignalResult("clean_structure", 0.800, 4),
}


def test_composite_matches_worked_example() -> None:
    base = composite_score(NVDA_SIGNALS_AT_10_42, ScoringWeights())
    # Spec 6.4: base_score = 77.46
    assert base == pytest.approx(77.46, abs=0.01)


def test_tod_multiplier_at_10_42_et() -> None:
    # 2026-04-15 10:42 ET → between 10:30 and 11:30 → 1.10
    now_ms = int(datetime(2026, 4, 15, 10, 42, tzinfo=ET).timestamp() * 1000)
    assert tod_multiplier(now_ms, DEFAULT_TOD_MULTIPLIERS) == pytest.approx(1.10)


def test_final_score_and_tier_match_worked_example() -> None:
    base = composite_score(NVDA_SIGNALS_AT_10_42, ScoringWeights())
    now_ms = int(datetime(2026, 4, 15, 10, 42, tzinfo=ET).timestamp() * 1000)
    mult = tod_multiplier(now_ms, DEFAULT_TOD_MULTIPLIERS)
    final = base * mult
    # Spec 6.5: final_score = 85.21
    assert final == pytest.approx(85.21, abs=0.02)
    # Spec 6.8: tier = "A"
    assert assign_tier(final, "long", TierThresholds()) == "A"


def test_tod_dst_spring_forward_2026_03_08() -> None:
    # 09:35 ET on the spring-forward day (DST starts at 02:00 → 03:00 ET).
    # Should still resolve to the 09:30–09:45 window (multiplier 0.85).
    now_ms = int(datetime(2026, 3, 8, 9, 35, tzinfo=ET).timestamp() * 1000)
    assert tod_multiplier(now_ms, DEFAULT_TOD_MULTIPLIERS) == pytest.approx(0.85)


def test_tod_dst_fall_back_2026_11_01() -> None:
    # 10:00 ET on fall-back day (DST ends at 02:00 → 01:00 ET).
    # Should resolve to the 09:45–10:30 window (multiplier 1.20).
    now_ms = int(datetime(2026, 11, 1, 10, 0, tzinfo=ET).timestamp() * 1000)
    assert tod_multiplier(now_ms, DEFAULT_TOD_MULTIPLIERS) == pytest.approx(1.20)


def test_tod_outside_session_returns_one() -> None:
    now_ms = int(datetime(2026, 4, 15, 8, 0, tzinfo=ET).timestamp() * 1000)
    assert tod_multiplier(now_ms, DEFAULT_TOD_MULTIPLIERS) == 1.0


@pytest.mark.parametrize(
    "score,bias,expected",
    [
        (90.0, "long", "A"),
        (85.0, "long", "A"),  # boundary inclusive
        (84.99, "long", "B"),
        (75.0, "long", "B"),  # boundary inclusive
        (74.99, "long", "C"),
        (75.0, "neutral", "C"),  # high-score neutral falls to C
        (70.0, "neutral", "C"),  # boundary inclusive
        (69.99, "long", None),
        (50.0, "short", None),
    ],
)
def test_tier_boundaries(score: float, bias: str, expected: str | None) -> None:
    assert assign_tier(score, bias, TierThresholds()) == expected  # type: ignore[arg-type]


# --- gate tests ---


def _gate_ctx(**overrides: object):  # noqa: ANN202 — test helper, not exported
    """Build a minimal SignalContext that passes all default gates."""
    from scanner.scoring.context import SignalContext
    from scanner.scoring.types import Bar

    last_bar = Bar(ts_ms=1, open=124.0, high=124.5, low=123.5, close=124.30, volume=1000.0)
    defaults: dict[str, object] = dict(
        symbol="NVDA",
        now_ms=1,
        session_start_ms=0,
        bars={"1m": [last_bar]},
        today_high=125.10,
        today_low=121.80,
        yesterday_high=124.50,
        yesterday_low=119.40,
        prior_session_close=122.10,
        today_cum_dollar_volume=128_000_000.0,
        historical_cum_dollar_volume_at_tod=52_000_000.0,
        historical_30min_dollar_volume=7_800_000.0,
        adv_dollar=480_000_000.0,
        atr_pct_20d=2.10,
        avg_spread_pct=0.04,
        halted_recently=False,
        last_1m_volume=10_000.0,
        avg_1m_volume_20bar=8_000.0,
        rolling_avg_spread_pct=0.04,
    )
    defaults.update(overrides)
    return SignalContext(**defaults)  # type: ignore[arg-type]


def test_gates_pass_baseline() -> None:
    ok, reasons = passes_gates(_gate_ctx(), GateThresholds())
    assert ok and reasons == []


def test_gates_fail_low_adv() -> None:
    ok, reasons = passes_gates(_gate_ctx(adv_dollar=1_000_000.0), GateThresholds())
    assert not ok
    assert any("adv_dollar" in r for r in reasons)


def test_gates_fail_low_atr() -> None:
    ok, reasons = passes_gates(_gate_ctx(atr_pct_20d=1.5), GateThresholds())
    assert not ok
    assert any("atr_pct_20d" in r for r in reasons)


def test_gates_fail_halted() -> None:
    ok, reasons = passes_gates(_gate_ctx(halted_recently=True), GateThresholds())
    assert not ok
    assert "halted_recently" in reasons
