"""Tests for scanner.engine."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from scanner.config import ScannerConfig, UniverseConfig
from scanner.engine import ScanResult, _weighted_sum, passes_universe_filter, scan
from scanner.indicators import enrich
from tests.conftest import make_ohlcv


def _bullish(volume_tail: float = 3_500_000.0, seed: int = 0) -> pd.DataFrame:
    """Rising trend with realistic pullbacks so RSI lands in the [50, 70] zone."""
    idx = pd.date_range(end=datetime(2025, 1, 1, tzinfo=UTC), periods=120, freq="B", tz="UTC")
    rng = np.random.default_rng(seed)
    returns = rng.normal(loc=0.003, scale=0.012, size=len(idx))
    close = pd.Series(50.0 * np.exp(np.cumsum(returns)), index=idx)
    vol = np.full(len(idx), 1_500_000.0)
    vol[-3:] = volume_tail
    return make_ohlcv(close, volume=pd.Series(vol, index=idx), intrabar=0.005)


def _bearish() -> pd.DataFrame:
    idx = pd.date_range(end=datetime(2025, 1, 1, tzinfo=UTC), periods=120, freq="B", tz="UTC")
    close = pd.Series(50.0 * np.exp(np.cumsum(np.full(len(idx), -0.008))), index=idx)
    return make_ohlcv(close, volume=1_500_000.0, intrabar=0.005)


def _noop_config() -> ScannerConfig:
    cfg = ScannerConfig()
    # Relax universe for fixtures (price ~50, volume 1.5M, dollar-vol ~75M — ok).
    return cfg


def test_bullish_fixture_scores_above_50() -> None:
    cfg = _noop_config()
    results = scan({"BULL": _bullish()}, cfg)
    assert len(results) == 1
    assert results[0].ticker == "BULL"
    assert results[0].score > 50.0


def test_bearish_fixture_filtered_out() -> None:
    cfg = _noop_config()
    results = scan({"BEAR": _bearish()}, cfg)
    assert results == []


def test_results_sorted_descending() -> None:
    cfg = _noop_config()
    data = {
        "STRONG": _bullish(volume_tail=4_000_000.0, seed=1),
        "WEAK": _bullish(volume_tail=1_700_000.0, seed=2),
        "BEAR": _bearish(),
    }
    results = scan(data, cfg)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_top_n_respected() -> None:
    cfg = _noop_config()
    cfg.top_n_results = 2
    data = {
        name: _bullish(volume_tail=1_800_000.0 + i * 200_000.0, seed=i)
        for i, name in enumerate("ABCDE")
    }
    results = scan(data, cfg)
    assert len(results) == 2


def test_passes_universe_filter_price_bounds() -> None:
    uni = UniverseConfig(min_price=10.0, max_price=500.0, min_avg_volume=0, min_dollar_volume=0.0)
    df = enrich(_bullish())
    assert passes_universe_filter(df, uni)


def test_weighted_sum_is_weighted_average() -> None:
    from scanner.config import ScoringWeights

    w = ScoringWeights()
    strengths = {
        "relative_volume": 1.0,
        "momentum": 1.0,
        "trend_alignment": 1.0,
        "rsi_position": 1.0,
        "breakout_proximity": 1.0,
    }
    assert _weighted_sum(strengths, w) == pytest.approx(1.0)


def test_scan_result_to_dict_rounds() -> None:
    r = ScanResult(
        ticker="X",
        score=72.12345,
        price=100.9876,
        pct_change=1.2345,
        rel_volume=2.3456,
        rsi=61.789,
        above_vwap=True,
        above_ema9=True,
        ema_stacked=True,
        dist_from_20d_high_pct=0.4321,
        signals={"momentum": 0.12345},
        reasons=["a", "b"],
    )
    d = r.to_dict()
    assert d["score"] == 72.12
    assert d["price"] == 100.99
    assert d["signals"]["momentum"] == 0.123
    assert d["reasons"] == ["a", "b"]
