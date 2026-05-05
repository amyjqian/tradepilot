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
    uni = UniverseConfig(min_price=10.0, min_avg_volume=0, min_dollar_volume=0.0)
    df = enrich(_bullish())
    assert passes_universe_filter(df, uni)


def test_pct_change_reanchored_to_daily_prior_close() -> None:
    # Reproduce the Monday-after-weekend case: the 1m lookback only contains
    # today's session, so the in-frame prior-close fallback uses today's open
    # ($558) and reports 4.84% on a $585 close — hiding the gap up from
    # Friday's $542 close. Daily bars include Friday → re-anchor should give
    # ~7.93%.
    today_idx = pd.date_range("2026-05-04 13:30", periods=60, freq="1min", tz="UTC")
    closes = np.linspace(558.0, 585.0, len(today_idx))  # walk from open to current
    intraday = pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.001,
            "low": closes * 0.999,
            "close": closes,
            "volume": [50_000.0] * len(today_idx),
        },
        index=today_idx,
    )
    intraday.loc[intraday.index[0], "open"] = 558.0  # explicit session open
    enriched = enrich(intraday)
    # Sanity: in-frame fallback is anchored to today's open.
    assert abs(enriched["pct_change"].iloc[-1] - ((585.0 - 558.0) / 558.0 * 100.0)) < 1e-6

    daily_idx = pd.date_range(end="2026-05-01", periods=20, freq="B", tz="UTC")
    daily_closes = np.linspace(500.0, 542.21, len(daily_idx))
    daily = pd.DataFrame(
        {
            "open": daily_closes,
            "high": daily_closes * 1.01,
            "low": daily_closes * 0.99,
            "close": daily_closes,
            "volume": [40_000_000.0] * len(daily_idx),
        },
        index=daily_idx,
    )

    from scanner.engine import _reanchor_pct_change_to_daily_prior

    _reanchor_pct_change_to_daily_prior(enriched, daily)
    expected = (585.0 - 542.21) / 542.21 * 100.0  # ~7.89%
    assert abs(enriched["pct_change"].iloc[-1] - expected) < 1e-6


def test_passes_universe_filter_uses_daily_when_provided() -> None:
    # Intraday-style scan frame with thin per-bar volume (1k shares/min) that
    # would fail a daily-tuned avg-volume floor of 500K. The daily liquidity
    # frame has the real picture (2M shares/day average) and should let it
    # through. Without `daily_df`, the same data is rejected.
    intraday_idx = pd.date_range(
        "2025-01-02 14:30", periods=30, freq="1min", tz="UTC"
    )
    intraday = make_ohlcv(
        pd.Series(100.0, index=intraday_idx), volume=1_000.0, intrabar=0.001
    )
    daily_idx = pd.date_range(end="2025-01-02", periods=25, freq="B", tz="UTC")
    daily = make_ohlcv(
        pd.Series(100.0, index=daily_idx), volume=2_000_000.0, intrabar=0.001
    )
    uni = UniverseConfig(
        min_price=1.0, min_avg_volume=500_000, min_dollar_volume=10_000_000
    )
    enriched = enrich(intraday)
    assert not passes_universe_filter(enriched, uni)  # in-bar fallback fails
    assert passes_universe_filter(enriched, uni, daily_df=daily)  # daily passes


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
