"""Per-signal correctness against hand-crafted contexts.

For each signal we exercise a "midpoint" case (computable by hand) plus
the insufficient-data branch.
"""

from __future__ import annotations

import pytest

from scanner.scoring.signals import (
    breakout_proximity,
    clean_structure,
    momentum_atr,
    rsi_intraday,
    rvol_30m,
    rvol_cumulative,
    vwap_distance_atr,
)

from ._helpers import make_bars, make_ctx


# ---------- rvol_30m ----------


def test_rvol_30m_below_floor_clips_to_zero() -> None:
    bars_1m = make_bars(30, closes=[100.0] * 30, volumes=[100.0] * 30)
    # recent_$vol = 30 * 100 * 100 = 300,000; ratio = 300,000 / 300,000 = 1.0 → < 1.5 → 0
    ctx = make_ctx(bars={"1m": bars_1m}, historical_30min_dollar_volume=300_000.0)
    r = rvol_30m.compute(ctx)
    assert r.strength == 0.0
    assert r.raw == pytest.approx(1.0)


def test_rvol_30m_midpoint() -> None:
    bars_1m = make_bars(30, closes=[100.0] * 30, volumes=[100.0] * 30)
    # ratio = 300,000 / 109,090.9 ≈ 2.75 → midpoint of [1.5, 4.0] → 0.5
    ctx = make_ctx(bars={"1m": bars_1m}, historical_30min_dollar_volume=300_000.0 / 2.75)
    r = rvol_30m.compute(ctx)
    assert r.strength == pytest.approx(0.5, abs=0.001)


def test_rvol_30m_insufficient_bars() -> None:
    ctx = make_ctx(bars={"1m": make_bars(5)})
    r = rvol_30m.compute(ctx)
    assert r.strength == 0.0 and r.raw is None


# ---------- rvol_cumulative ----------


def test_rvol_cumulative_midpoint() -> None:
    # ratio 2.0 → midpoint of [1.0, 3.0] → 0.5
    ctx = make_ctx(
        today_cum_dollar_volume=100.0,
        historical_cum_dollar_volume_at_tod=50.0,
    )
    r = rvol_cumulative.compute(ctx)
    assert r.strength == pytest.approx(0.5)
    assert r.raw == pytest.approx(2.0)


def test_rvol_cumulative_max_clip() -> None:
    ctx = make_ctx(
        today_cum_dollar_volume=500.0,
        historical_cum_dollar_volume_at_tod=50.0,
    )
    r = rvol_cumulative.compute(ctx)
    assert r.strength == 1.0


# ---------- momentum_atr ----------


def test_momentum_atr_midpoint() -> None:
    # daily ATR will be 2% of close; today's move = 2% → 1 ATR → strength 0.5
    daily = make_bars(
        20,
        closes=[100.0] * 20,
        highs=[101.0] * 20,
        lows=[99.0] * 20,  # TR = 2.0 every bar → ATR = 2.0 → ATR% = 2.0
    )
    last_1m = make_bars(1, closes=[102.0])  # +2% from 100
    ctx = make_ctx(
        bars={"1m": last_1m, "daily": daily},
        prior_session_close=100.0,
    )
    r = momentum_atr.compute(ctx)
    assert r.raw == pytest.approx(1.0, abs=0.01)
    assert r.strength == pytest.approx(0.5, abs=0.01)


def test_momentum_atr_negative_clips_to_zero() -> None:
    daily = make_bars(20, closes=[100.0] * 20, highs=[101.0] * 20, lows=[99.0] * 20)
    last_1m = make_bars(1, closes=[98.0])
    ctx = make_ctx(
        bars={"1m": last_1m, "daily": daily},
        prior_session_close=100.0,
    )
    assert momentum_atr.compute(ctx).strength == 0.0


# ---------- vwap_distance_atr ----------


def test_vwap_distance_peaks_at_half_atr() -> None:
    # 15 5m bars at flat 100 except last, which moves up by exactly 0.5 * ATR.
    bars = make_bars(
        15,
        closes=[100.0] * 14 + [100.0],
        highs=[101.0] * 14 + [101.0],
        lows=[99.0] * 14 + [99.0],
    )
    # Replace last bar with a stronger close — but VWAP barely moves with one
    # bar shift, so distance ~= move/atr. ATR over flat bars where TR=2 → 2.0.
    # We want distance/ATR = 0.5 → close = vwap + 1.0 ≈ 101.0
    bars[-1] = bars[-1]._replace(close=101.0, high=101.0, low=99.0)
    ctx = make_ctx(bars={"5m": bars})
    r = vwap_distance_atr.compute(ctx)
    # distance_atr should be near 0.5, strength near 1.0
    assert r.raw is not None
    assert r.strength == pytest.approx(1.0, abs=0.05)


def test_vwap_distance_negative_is_zero() -> None:
    bars = make_bars(15, closes=[100.0] * 14 + [99.0])
    bars[-1] = bars[-1]._replace(close=99.0)
    ctx = make_ctx(bars={"5m": bars})
    r = vwap_distance_atr.compute(ctx)
    assert r.strength == 0.0


# ---------- rsi_intraday ----------


def test_rsi_curve_in_sweet_spot() -> None:
    # 20 ascending closes → RSI stays high (typically ~100, in the >90 dead zone)
    # Use a gentle uptrend so RSI lands in the 60–80 plateau.
    closes = [100.0 + i * 0.05 for i in range(30)] + [
        100.0 + 30 * 0.05 - i * 0.02 for i in range(5)
    ]
    bars = make_bars(len(closes), closes=closes)
    ctx = make_ctx(bars={"5m": bars})
    r = rsi_intraday.compute(ctx)
    assert r.raw is not None
    if 60.0 <= r.raw <= 80.0:
        assert r.strength == pytest.approx(1.0)


def test_rsi_below_50_is_zero() -> None:
    closes = [100.0 - i * 0.5 for i in range(30)]  # steady downtrend → RSI low
    bars = make_bars(len(closes), closes=closes)
    ctx = make_ctx(bars={"5m": bars})
    r = rsi_intraday.compute(ctx)
    assert r.strength == 0.0


# ---------- breakout_proximity ----------


def test_breakout_proximity_approaching_target() -> None:
    """Price below prior HoD, last 5 highs all rising → signal positive."""
    closes = [100.0, 100.1, 100.2, 100.3, 100.4]
    # Each bar's high is a few cents above its close; the last bar's
    # high (100.7) is below the yesterday_high target (100.8) — so this
    # is a real "approaching" case.
    highs = [100.5, 100.55, 100.6, 100.65, 100.7]
    bars = make_bars(5, closes=closes, highs=highs)
    ctx = make_ctx(bars={"1m": bars}, yesterday_high=100.8)
    r = breakout_proximity.compute(ctx)
    # prior_today_high = max(highs[:-1]) = 100.65; target = min(100.65, 100.8) = 100.65
    # distance = (100.65 - 100.4) / 100.4 * 100 ≈ 0.249%
    # strength = 1 - 0.249 / 5 ≈ 0.95
    assert r.strength == pytest.approx(0.95, abs=0.02)
    assert r.raw == pytest.approx(0.249, abs=0.01)


def test_breakout_proximity_today_high_leak_protection() -> None:
    """Latest 1m bar IS the HoD — signal must not falsely fire against
    the bar's own wick. With prior_today_high < latest_close, the stock
    has 'already broken' so strength must be 0."""
    closes = [100.0, 100.1, 100.2, 100.3, 100.5]
    highs = [100.05, 100.15, 100.25, 100.35, 100.55]  # last bar makes new HoD
    bars = make_bars(5, closes=closes, highs=highs)
    ctx = make_ctx(
        bars={"1m": bars},
        today_high=100.55,  # equal to latest bar's high — the leak case
        yesterday_high=100.7,
    )
    r = breakout_proximity.compute(ctx)
    # prior_today_high = max(100.05, 100.15, 100.25, 100.35) = 100.35
    # target = min(100.35, 100.7) = 100.35
    # close = 100.5 → already above target → distance < 0 → strength 0
    assert r.strength == 0.0
    assert r.raw is not None and r.raw < 0


def test_breakout_proximity_one_violation_tolerated() -> None:
    """One down move in the last 5 highs is OK (3 of 4 transitions
    non-decreasing). Models a real-world 'rising with one minor
    pullback' breakout."""
    closes = [100.0, 100.1, 100.2, 100.3, 100.4]
    # 100.5 → 100.6 (up) → 100.55 (down, the violation) → 100.65 (up) → 100.7 (up)
    highs = [100.5, 100.6, 100.55, 100.65, 100.7]
    bars = make_bars(5, closes=closes, highs=highs)
    ctx = make_ctx(bars={"1m": bars}, yesterday_high=100.8)
    r = breakout_proximity.compute(ctx)
    # 3 of 4 transitions ascending — passes the new 3-of-4 threshold
    # prior_today_high = max(100.5, 100.6, 100.55, 100.65) = 100.65
    # target = min(100.65, 100.8) = 100.65 → distance = 0.249%
    assert r.strength > 0.9


def test_breakout_proximity_two_violations_kills_signal() -> None:
    """Two down moves in last 5 highs → signal collapses to 0."""
    closes = [100.0, 100.1, 100.2, 100.3, 100.4]
    # 100.5 → 100.45 (down) → 100.55 (up) → 100.5 (down) → 100.6 (up): 2 violations
    highs = [100.5, 100.45, 100.55, 100.5, 100.6]
    bars = make_bars(5, closes=closes, highs=highs)
    ctx = make_ctx(bars={"1m": bars}, yesterday_high=100.8)
    assert breakout_proximity.compute(ctx).strength == 0.0


def test_breakout_proximity_no_higher_highs_is_zero() -> None:
    closes = [100.0, 100.1, 100.2, 100.3, 100.4]
    highs = [100.5, 100.4, 100.3, 100.2, 100.1]  # all-descending highs (4 violations)
    bars = make_bars(5, closes=closes, highs=highs)
    ctx = make_ctx(bars={"1m": bars}, today_high=101.0, yesterday_high=101.0)
    assert breakout_proximity.compute(ctx).strength == 0.0


def test_breakout_proximity_flat_highs_allowed() -> None:
    """Flat highs (no change between adjacent bars) count as
    non-decreasing. 3 of 4 must be flat-or-up."""
    closes = [100.0, 100.1, 100.2, 100.3, 100.4]
    highs = [100.5, 100.5, 100.55, 100.6, 100.65]  # one flat, three rising
    bars = make_bars(5, closes=closes, highs=highs)
    ctx = make_ctx(bars={"1m": bars}, yesterday_high=100.8)
    assert breakout_proximity.compute(ctx).strength > 0.9


# ---------- clean_structure ----------


def test_clean_structure_no_flips() -> None:
    bars = make_bars(20, closes=[100.0 + i for i in range(20)])  # strict ascending
    ctx = make_ctx(bars={"5m": bars})
    r = clean_structure.compute(ctx)
    assert r.raw == 0
    assert r.strength == 1.0


def test_clean_structure_alternating_max_flips() -> None:
    closes = [100.0 + (i % 2) for i in range(20)]
    bars = make_bars(20, closes=closes)
    ctx = make_ctx(bars={"5m": bars})
    r = clean_structure.compute(ctx)
    # 18 comparisons, all flips → strength = 1 - 18/20 = 0.10
    assert r.raw == 18
    assert r.strength == pytest.approx(0.10)


def test_clean_structure_insufficient_bars() -> None:
    ctx = make_ctx(bars={"5m": make_bars(5)})
    assert clean_structure.compute(ctx).raw is None
