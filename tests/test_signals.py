"""Tests for scanner.signals."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from scanner.config import SignalThresholds
from scanner.indicators import enrich
from scanner.signals import (
    apply_hard_filters,
    breakout_proximity_signal,
    momentum_signal,
    relative_volume_signal,
    rsi_position_signal,
    trend_alignment_signal,
)
from tests.conftest import make_ohlcv


@pytest.fixture
def cfg() -> SignalThresholds:
    return SignalThresholds()


def _bullish_frame() -> pd.DataFrame:
    idx = pd.date_range(end=datetime(2025, 1, 1, tzinfo=UTC), periods=120, freq="B", tz="UTC")
    close = pd.Series(100.0 * np.exp(np.cumsum(np.full(len(idx), 0.008))), index=idx)
    vol = np.full(len(idx), 1_000_000.0)
    vol[-3:] = 3_500_000.0
    df = make_ohlcv(close, volume=pd.Series(vol, index=idx), intrabar=0.005)
    return enrich(df)


def _bearish_frame() -> pd.DataFrame:
    idx = pd.date_range(end=datetime(2025, 1, 1, tzinfo=UTC), periods=120, freq="B", tz="UTC")
    close = pd.Series(100.0 * np.exp(np.cumsum(np.full(len(idx), -0.008))), index=idx)
    df = make_ohlcv(close, volume=1_000_000.0, intrabar=0.005)
    return enrich(df)


def test_all_signals_return_in_01(cfg: SignalThresholds) -> None:
    df = _bullish_frame()
    for fn in (
        relative_volume_signal,
        momentum_signal,
        rsi_position_signal,
        breakout_proximity_signal,
    ):
        v = fn(df, cfg)
        assert 0.0 <= v <= 1.0, f"{fn.__name__} returned {v}"
    ta, br = trend_alignment_signal(df, cfg)
    assert 0.0 <= ta <= 1.0
    assert set(br) == {"above_vwap", "above_ema9", "ema9_gt_ema20", "ema20_gt_ema50"}


def test_momentum_zero_on_down_day(cfg: SignalThresholds) -> None:
    df = _bearish_frame()
    assert momentum_signal(df, cfg) == 0.0


def test_trend_alignment_full_on_bullish_fixture(cfg: SignalThresholds) -> None:
    df = _bullish_frame()
    ta, br = trend_alignment_signal(df, cfg)
    assert all(br.values()), br
    assert ta == 1.0


def test_trend_alignment_zero_on_bearish_fixture(cfg: SignalThresholds) -> None:
    df = _bearish_frame()
    ta, br = trend_alignment_signal(df, cfg)
    # All conditions should fail on a steady downtrend.
    assert ta == 0.0, br


def test_rsi_peaks_at_midpoint(cfg: SignalThresholds) -> None:
    # Build an enriched frame and manually pin rsi14 on last bar to test curve.
    df = _bullish_frame().copy()
    midpoint = (cfg.rsi_min + cfg.rsi_max) / 2.0  # 60
    df.loc[df.index[-1], "rsi14"] = midpoint
    assert rsi_position_signal(df, cfg) == pytest.approx(1.0)

    df.loc[df.index[-1], "rsi14"] = cfg.rsi_min
    assert rsi_position_signal(df, cfg) == pytest.approx(0.0, abs=1e-9)

    df.loc[df.index[-1], "rsi14"] = cfg.rsi_max
    assert rsi_position_signal(df, cfg) == pytest.approx(0.0, abs=1e-9)

    # Above rsi_max but below 85 → partial decay.
    df.loc[df.index[-1], "rsi14"] = 77.5
    v = rsi_position_signal(df, cfg)
    assert 0.0 < v < 1.0

    # Overbought fade hits zero at RSI=85.
    df.loc[df.index[-1], "rsi14"] = 85.0
    assert rsi_position_signal(df, cfg) == 0.0


def test_breakout_proximity_curve(cfg: SignalThresholds) -> None:
    df = _bullish_frame().copy()
    df.loc[df.index[-1], "dist_from_high_20"] = 0.0
    assert breakout_proximity_signal(df, cfg) == pytest.approx(1.0)

    df.loc[df.index[-1], "dist_from_high_20"] = 2.5
    assert breakout_proximity_signal(df, cfg) == pytest.approx(0.5)

    df.loc[df.index[-1], "dist_from_high_20"] = 10.0
    assert breakout_proximity_signal(df, cfg) == 0.0


def test_relative_volume_zero_at_threshold(cfg: SignalThresholds) -> None:
    df = _bullish_frame().copy()
    df.loc[df.index[-1], "rel_volume"] = cfg.min_relative_volume
    assert relative_volume_signal(df, cfg) == 0.0

    df.loc[df.index[-1], "rel_volume"] = 3.0
    assert relative_volume_signal(df, cfg) == pytest.approx(1.0)


def test_hard_filters_pass_on_bullish(cfg: SignalThresholds) -> None:
    df = _bullish_frame()
    assert apply_hard_filters(df, cfg) is True


def test_hard_filters_fail_on_bearish(cfg: SignalThresholds) -> None:
    df = _bearish_frame()
    assert apply_hard_filters(df, cfg) is False
