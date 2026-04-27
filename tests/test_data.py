"""Tests for scanner.data — BarCache, CachedProvider, synthetic determinism, validate_bars."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from scanner.data import (
    BarCache,
    CachedProvider,
    MarketDataProvider,
    SyntheticProvider,
    get_provider,
    validate_bars,
)


def _ohlcv(periods: int = 60) -> pd.DataFrame:
    idx = pd.date_range(end=datetime(2025, 1, 1, tzinfo=UTC), periods=periods, freq="B", tz="UTC")
    rng = np.random.default_rng(0)
    close = 100.0 + np.cumsum(rng.normal(0, 1, periods))
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(periods, 1_000_000.0),
        },
        index=idx,
    )


def test_validate_bars_ok() -> None:
    df = _ohlcv(30)
    assert validate_bars(df) is df


def test_validate_bars_rejects_missing_columns() -> None:
    df = _ohlcv(10).drop(columns=["volume"])
    with pytest.raises(ValueError, match="Missing OHLCV columns"):
        validate_bars(df)


def test_validate_bars_rejects_naive_index() -> None:
    df = _ohlcv(10)
    df.index = df.index.tz_localize(None)
    with pytest.raises(ValueError, match="tz-aware"):
        validate_bars(df)


def test_validate_bars_rejects_unsorted() -> None:
    df = _ohlcv(10)
    df = df.iloc[::-1]
    with pytest.raises(ValueError, match="sorted ascending"):
        validate_bars(df)


def test_validate_bars_rejects_nan() -> None:
    df = _ohlcv(10).copy()
    df.iloc[5, df.columns.get_loc("close")] = np.nan
    with pytest.raises(ValueError, match="must not contain NaN"):
        validate_bars(df)


def test_synthetic_provider_deterministic() -> None:
    p1 = SyntheticProvider()
    p2 = SyntheticProvider()
    a = p1.get_bars("FOO", "1d", 30)
    b = p2.get_bars("FOO", "1d", 30)
    pd.testing.assert_frame_equal(a, b)


def test_synthetic_provider_batch() -> None:
    p = SyntheticProvider()
    batch = p.get_bars_batch(["A", "B", "C"], "1d", 30)
    assert set(batch.keys()) == {"A", "B", "C"}
    for df in batch.values():
        assert len(df) >= 20


def test_synthetic_provider_intraday_shape() -> None:
    p = SyntheticProvider()
    df = p.get_bars("X", "5m", 2)
    assert len(df) > 10
    assert df.index.tz is not None


def test_get_provider_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("banana")


def test_bar_cache_roundtrip(tmp_path) -> None:
    cache = BarCache(tmp_path / "bars")
    df = _ohlcv(30)
    assert cache.get("AAPL", "1d") is None
    cache.put("AAPL", "1d", df)
    got = cache.get("AAPL", "1d")
    assert got is not None
    # Parquet loses the BusinessDay freq metadata; compare values/shape instead.
    assert len(got) == len(df)
    for col in ("open", "high", "low", "close", "volume"):
        assert (got[col].values == df[col].values).all()


class _CountingProvider(MarketDataProvider):
    def __init__(self, inner: MarketDataProvider) -> None:
        self._inner = inner
        self.calls: int = 0
        self.last_lookback: int | None = None

    def get_bars(self, ticker: str, interval: str, lookback_days: int) -> pd.DataFrame:
        self.calls += 1
        self.last_lookback = lookback_days
        return self._inner.get_bars(ticker, interval, lookback_days)

    def get_bars_batch(
        self, tickers: list[str], interval: str, lookback_days: int
    ) -> dict[str, pd.DataFrame]:
        self.calls += len(tickers)
        self.last_lookback = lookback_days
        return self._inner.get_bars_batch(tickers, interval, lookback_days)


def test_cached_provider_serves_fresh_cache_without_fetch(tmp_path) -> None:
    """When cache is fresh (last bar within TTL) the wrapped provider isn't touched."""
    # End the synthetic series "now" so the cached last bar is fresh on read.
    now = datetime.now(UTC)
    counting = _CountingProvider(SyntheticProvider(end=now))
    wrapped = CachedProvider(counting, BarCache(tmp_path / "cache"))

    df1 = wrapped.get_bars("AAPL", "1d", 30)
    df2 = wrapped.get_bars("AAPL", "1d", 30)
    assert counting.calls == 1  # second call served from cache — no re-fetch
    tail = min(len(df1), len(df2))
    assert (df1["close"].iloc[-tail:].values == df2["close"].iloc[-tail:].values).all()


def test_cached_provider_delta_fetches_when_stale(tmp_path) -> None:
    """Stale cache with recent data triggers a small delta fetch, not a full re-download."""
    cache = BarCache(tmp_path / "cache")
    # Prime the cache with a frame whose last bar is ~2 days old. Simulates
    # a run we did a couple of days ago.
    end = datetime.now(UTC)
    idx = pd.date_range(end=end - pd.Timedelta(days=2), periods=30, freq="B", tz="UTC")
    df = pd.DataFrame(
        {c: np.arange(30, dtype=float) for c in ("open", "high", "low", "close", "volume")},
        index=idx,
    )
    cache.put("AAPL", "1d", df)

    counting = _CountingProvider(SyntheticProvider(end=end))
    # Force staleness instantly so the TTL path doesn't short-circuit.
    wrapped = CachedProvider(counting, cache, staleness_ttl_sec={"1d": 0.001})

    wrapped.get_bars("AAPL", "1d", 30)
    # Cache exists + stale → delta fetch sized to cover ~2-day gap, not a full 30d.
    assert counting.calls == 1
    assert counting.last_lookback is not None
    assert counting.last_lookback < 10, (
        f"expected a small delta lookback, got {counting.last_lookback}"
    )


def test_cached_provider_merges_on_overlap_preferring_new(tmp_path) -> None:
    """Merged frame takes new bar's values for timestamps present in both."""
    cache = BarCache(tmp_path / "cache")
    idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
    old = pd.DataFrame(
        {"open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0], "low": [1.0, 2.0, 3.0],
         "close": [1.0, 2.0, 3.0], "volume": [10.0, 20.0, 30.0]},
        index=idx,
    )
    cache.put("X", "1d", old)

    # Stub provider returns the overlapping last timestamp with a *different*
    # close, plus one new timestamp beyond it.
    new_idx = pd.date_range("2026-01-03", periods=2, freq="D", tz="UTC")
    new = pd.DataFrame(
        {"open": [9.0, 4.0], "high": [9.0, 4.0], "low": [9.0, 4.0],
         "close": [9.0, 4.0], "volume": [99.0, 40.0]},
        index=new_idx,
    )

    class _Stub(MarketDataProvider):
        def get_bars(self, ticker, interval, lookback_days):
            return new
        def get_bars_batch(self, tickers, interval, lookback_days):
            return {t: new for t in tickers}

    wrapped = CachedProvider(_Stub(), cache, staleness_ttl_sec={"1d": 0.001})
    result = wrapped.get_bars("X", "1d", 10)

    # 2026-01-03 should reflect new's close (9.0), not old's (3.0).
    ts_overlap = pd.Timestamp("2026-01-03", tz="UTC")
    assert result.loc[ts_overlap, "close"] == 9.0
    # And 2026-01-04 should be present from new.
    assert pd.Timestamp("2026-01-04", tz="UTC") in result.index


def test_cached_provider_falls_back_to_stale_cache_on_pacing(tmp_path) -> None:
    """When the wrapped provider raises PacingBudgetExhausted and cache
    exists, serve the cached bars instead of failing the whole scan."""
    from scanner.data.ib_provider import PacingBudgetExhausted

    cache = BarCache(tmp_path / "cache")
    idx = pd.date_range(end=datetime.now(UTC), periods=5, freq="D", tz="UTC")
    cached_frame = pd.DataFrame(
        {c: np.arange(5, dtype=float) for c in ("open", "high", "low", "close", "volume")},
        index=idx,
    )
    cache.put("AAPL", "1d", cached_frame)
    cache.put("MSFT", "1d", cached_frame)

    class _PacingBlocked(MarketDataProvider):
        def get_bars(self, ticker, interval, lookback_days):
            raise PacingBudgetExhausted("full", retry_after_sec=300)
        def get_bars_batch(self, tickers, interval, lookback_days):
            raise PacingBudgetExhausted("full", retry_after_sec=300)

    wrapped = CachedProvider(
        _PacingBlocked(), cache,
        staleness_ttl_sec={"1d": 0.001},  # force stale so fetch is attempted
    )

    # Single-ticker path
    df = wrapped.get_bars("AAPL", "1d", 5)
    assert not df.empty

    # Batch path
    batch = wrapped.get_bars_batch(["AAPL", "MSFT"], "1d", 5)
    assert set(batch) == {"AAPL", "MSFT"}

    # And when the cache has nothing for a ticker, the exception still
    # surfaces so the server returns 429.
    cache2 = BarCache(tmp_path / "cache2")
    wrapped2 = CachedProvider(_PacingBlocked(), cache2)
    with pytest.raises(PacingBudgetExhausted):
        wrapped2.get_bars_batch(["NEW"], "1d", 5)


def test_cached_provider_batch(tmp_path) -> None:
    now = datetime.now(UTC)
    wrapped = CachedProvider(SyntheticProvider(end=now), BarCache(tmp_path / "cache"))
    batch1 = wrapped.get_bars_batch(["A", "B"], "1d", 30)
    batch2 = wrapped.get_bars_batch(["A", "B", "C"], "1d", 30)
    assert set(batch1) == {"A", "B"}
    assert set(batch2) == {"A", "B", "C"}


def test_get_provider_with_cache(tmp_path) -> None:
    p = get_provider("synthetic", cache_path=tmp_path / "cache")
    df = p.get_bars("ABC", "1d", 30)
    assert len(df) > 20
