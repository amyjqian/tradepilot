"""Tests for scanner.indicators."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from scanner.indicators import (
    ENRICHED_COLUMNS,
    atr,
    bollinger_bands,
    distance_from_high,
    ema,
    enrich,
    gap_pct,
    pct_change_today,
    relative_volume,
    rsi,
    sma,
    vwap,
)
from tests.conftest import make_ohlcv


def test_rsi_stays_in_range(random_walk_close: pd.Series) -> None:
    r = rsi(random_walk_close, period=14).dropna()
    assert r.min() >= 0.0
    assert r.max() <= 100.0


def test_rsi_flat_is_neutral(constant_series: pd.Series) -> None:
    r = rsi(constant_series, period=14).dropna()
    # On a perfectly flat series avg gain and avg loss are both 0 → RSI = 50 by convention.
    assert (r == 50.0).all()


def test_ema_of_constant_is_constant(constant_series: pd.Series) -> None:
    e = ema(constant_series, 9)
    assert np.allclose(e.to_numpy(), 100.0)


def test_sma_of_constant_is_constant(constant_series: pd.Series) -> None:
    s = sma(constant_series, 5).dropna()
    assert np.allclose(s.to_numpy(), 100.0)


def test_relative_volume_constant(daily_index: pd.DatetimeIndex) -> None:
    close = pd.Series(100.0, index=daily_index)
    df = make_ohlcv(close, volume=500_000.0, intrabar=0.001)
    rv = relative_volume(df, period=20).dropna()
    assert np.allclose(rv.to_numpy(), 1.0)


def test_gap_pct_sign(daily_index: pd.DatetimeIndex) -> None:
    close = pd.Series(100.0, index=daily_index)
    df = make_ohlcv(close, intrabar=0.001).copy()
    # Force a 2% gap up on the last bar.
    df.loc[df.index[-1], "open"] = df["close"].iloc[-2] * 1.02
    g = gap_pct(df).iloc[-1]
    assert abs(g - 2.0) < 1e-6


def test_pct_change_today_zero_on_flat(constant_series: pd.Series) -> None:
    df = make_ohlcv(constant_series, intrabar=0.001)
    c = pct_change_today(df).dropna()
    assert np.allclose(c.to_numpy(), 0.0)


def test_pct_change_today_intraday_anchors_to_prior_close() -> None:
    # Two 5-bar sessions on consecutive days. Session 1 closes at 102 after
    # opening at 100. Session 2 gaps up to 110 at the open and walks to 114.
    # With prior-close anchoring:
    #   - session 1 has no prior session → falls back to its own open (100).
    #   - session 2 anchors to session 1's last close (102), so the gap is
    #     captured in the very first bar's reading rather than hidden.
    d1 = pd.date_range("2025-01-02 14:30", periods=5, freq="1min", tz="UTC")
    d2 = pd.date_range("2025-01-03 14:30", periods=5, freq="1min", tz="UTC")
    idx = d1.append(d2)
    closes = [100.0, 100.5, 101.0, 101.5, 102.0, 110.0, 111.0, 112.0, 113.0, 114.0]
    df = pd.DataFrame(
        {
            "open": [100.0] * 5 + [110.0] * 5,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": [1_000.0] * 10,
        },
        index=idx,
    )
    c = pct_change_today(df)
    # First session falls back to its own open: bar 0 close==open → 0%.
    assert abs(c.iloc[0] - 0.0) < 1e-9
    # Last bar of session 1 vs session-1 open: (102 - 100) / 100 * 100 = 2%.
    assert abs(c.iloc[4] - 2.0) < 1e-9
    # First bar of session 2 vs prior close (102): (110 - 102) / 102 * 100
    # ≈ 7.843% — the gap is included from the very first bar.
    assert abs(c.iloc[5] - ((110.0 - 102.0) / 102.0 * 100.0)) < 1e-9
    # Last bar of session 2 vs prior close (102): (114 - 102) / 102 * 100
    # ≈ 11.765%.
    assert abs(c.iloc[9] - ((114.0 - 102.0) / 102.0 * 100.0)) < 1e-9


def test_atr_positive(random_walk_close: pd.Series) -> None:
    df = make_ohlcv(random_walk_close, intrabar=0.01)
    a = atr(df, 14).dropna()
    assert (a > 0).all()


def test_distance_from_high_nonnegative(random_walk_close: pd.Series) -> None:
    df = make_ohlcv(random_walk_close, intrabar=0.01)
    d = distance_from_high(df, 20).dropna()
    assert (d >= -1e-9).all()


def test_bollinger_bands_ordering(random_walk_close: pd.Series) -> None:
    lo, mid, up = bollinger_bands(random_walk_close, 20, 2.0)
    df = pd.DataFrame({"lo": lo, "mid": mid, "up": up}).dropna()
    assert (df["lo"] <= df["mid"]).all()
    assert (df["mid"] <= df["up"]).all()


def test_vwap_intraday_resets() -> None:
    # Two sessions, 10 minutes each.
    d1 = pd.date_range("2025-01-02 14:30", periods=10, freq="1min", tz="UTC")
    d2 = pd.date_range("2025-01-03 14:30", periods=10, freq="1min", tz="UTC")
    idx = d1.append(d2)
    close = pd.Series(100.0, index=idx)
    df = make_ohlcv(close, volume=1_000.0, intrabar=0.001)
    v = vwap(df)
    # First bar of each session: cumulative is just that bar → VWAP = typical price.
    first_s1 = v.iloc[0]
    first_s2 = v.iloc[10]
    assert abs(first_s1 - first_s2) < 1e-6  # both first bars, same typical price


def test_enrich_adds_all_columns(random_walk_close: pd.Series) -> None:
    df = make_ohlcv(random_walk_close, intrabar=0.01)
    out = enrich(df)
    for col in ENRICHED_COLUMNS:
        assert col in out.columns, f"missing {col}"
    # Must not drop rows.
    assert len(out) == len(df)
    # Original OHLCV preserved.
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


@pytest.mark.parametrize("period", [9, 20, 50])
def test_ema_monotone_on_rising_series(period: int) -> None:
    idx = pd.date_range(end=datetime(2025, 1, 1, tzinfo=UTC), periods=100, freq="B", tz="UTC")
    rising = pd.Series(np.linspace(50.0, 150.0, 100), index=idx)
    e = ema(rising, period).dropna()
    diffs = np.diff(e.to_numpy())
    assert (diffs > 0).all()
