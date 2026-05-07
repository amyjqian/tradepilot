"""List-based indicator helpers."""

from __future__ import annotations

import pytest

from scanner.scoring.indicators import (
    atr_pct,
    atr_value,
    clip,
    ema,
    rsi,
    trend_stack_count,
    vwap_session,
)
from scanner.scoring.types import Bar


def test_clip() -> None:
    assert clip(0.5, 0.0, 1.0) == 0.5
    assert clip(-0.1, 0.0, 1.0) == 0.0
    assert clip(2.0, 0.0, 1.0) == 1.0


def test_ema_seeds_with_simple_average() -> None:
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)  # mean of [1,2,3]


def test_rsi_first_period_indices_are_none() -> None:
    out = rsi([float(i) for i in range(20)], 14)
    assert all(v is None for v in out[:14])
    assert out[14] is not None


def test_rsi_strict_uptrend_is_high() -> None:
    out = rsi([100.0 + i for i in range(30)], 14)
    assert out[-1] is not None
    assert out[-1] > 99.0


def test_atr_value_constant_tr() -> None:
    bars = [Bar(i, 100.0, 101.0, 99.0, 100.0, 1000.0) for i in range(20)]
    out = atr_value(bars, 14)
    # Every TR = 2.0 → ATR = 2.0
    assert out[-1] == pytest.approx(2.0)


def test_atr_pct() -> None:
    bars = [Bar(i, 100.0, 101.0, 99.0, 100.0, 1000.0) for i in range(20)]
    out = atr_pct(bars, 14)
    assert out[-1] == pytest.approx(2.0)  # ATR=2 / close=100 * 100


def test_vwap_session_anchors() -> None:
    # First two bars are pre-session (filtered out), last two count.
    bars = [
        Bar(0, 100.0, 101.0, 99.0, 100.0, 1000.0),
        Bar(1, 200.0, 201.0, 199.0, 200.0, 1000.0),
        Bar(10, 300.0, 301.0, 299.0, 300.0, 1000.0),  # typical = 300
        Bar(11, 400.0, 401.0, 399.0, 400.0, 1000.0),  # typical = 400
    ]
    out = vwap_session(bars, session_start_ms=10)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(300.0)
    assert out[3] == pytest.approx(350.0)


def test_trend_stack_count_full_stack() -> None:
    # 60 bars, monotonic ascending → all four checks true → count 4
    bars = [Bar(i, float(i), float(i) + 0.5, float(i) - 0.5, float(i), 1000.0) for i in range(60)]
    result = trend_stack_count(bars, session_start_ms=0)
    assert result is not None
    strength, count = result
    assert count == 4
    assert strength == 1.0


def test_trend_stack_count_insufficient_bars() -> None:
    bars = [Bar(i, 100.0, 100.5, 99.5, 100.0, 1000.0) for i in range(10)]
    assert trend_stack_count(bars, session_start_ms=0) is None
