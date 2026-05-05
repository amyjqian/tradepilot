"""On-disk parquet cache for OHLCV bars + a CachedProvider with delta fetches.

Persistence: one parquet per (ticker, interval) pair under the cache dir.
Survives restarts. On repeated scans the wrapped provider is only called when
the cached data is stale — and even then only for the delta since the last
cached bar, not a full re-download. For IB this matters because every fetch
costs a pacing slot (55 per 10-minute window); delta merges keep that budget
from being chewed through on repeated scans.

Freshness is judged by the last cached bar's timestamp vs. `now`, per
interval. If the last bar is within the TTL we don't fetch at all — the
cached frame is served. If older, we fetch a delta sized to just cover the
gap plus a one-day safety overlap, merge (new wins on overlapping timestamps
so partial last-bars get replaced by their final values), write back, and
serve the merged frame.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import date
from pathlib import Path

import pandas as pd

from scanner.data.base import MarketDataProvider, validate_bars

log = logging.getLogger(__name__)


# How long the on-disk cache is considered authoritative before we go back
# to the wrapped provider for a delta. Defaults are tuned so normal scan
# cadence (a click every few minutes) reuses cache — each IB fetch costs a
# pacing slot regardless of size, so short TTLs burn the 55/600s budget
# fast. Roughly one bar-interval of staleness is acceptable for scoring.
_STALENESS_TTL_SEC: dict[str, float] = {
    "1m": 60.0,         # one bar
    "5m": 300.0,        # one bar
    "15m": 900.0,       # one bar
    "1h": 3600.0,       # one bar
    "1d": 86400.0,      # one bar (24h)
}


def _ttl_for(interval: str, overrides: dict[str, float] | None = None) -> float:
    if overrides and interval in overrides:
        return overrides[interval]
    return _STALENESS_TTL_SEC.get(interval, 120.0)


# Polygon's date-ranged fetch is calendar-based: a request that starts on
# 2026-04-04 (Saturday) returns the first trading bar on 2026-04-06. The
# cache's `index[0]` is then permanently later than the calendar `wanted_start`
# we compute from `now - lookback_days`, and the left-edge check below
# would treat that as "missing data" and refetch the full window on every
# scan — even though Polygon won't ever produce earlier bars, because no
# trading happened. Same story for holidays, post-IPO listings, or any
# non-RTH gap at the lookback boundary. Allowing four calendar days of
# slack covers any 3-day-weekend + a one-day buffer; the worst case if the
# cache really is short some genuine bars is that the chart shows a little
# less history than asked, which beats the network stampede.
_LEFT_EDGE_SLACK = pd.Timedelta(days=4)


def _delta_lookback_days(last_ts: pd.Timestamp, now: pd.Timestamp) -> int:
    """Pick a lookback that safely covers (last_ts, now] plus a day of overlap."""
    seconds = max(0.0, (now - last_ts).total_seconds())
    return max(1, int(math.ceil(seconds / 86400.0)) + 1)


def _merge_bars(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Union old+new on the timestamp index, preferring `new` on overlap.

    The caller should strip `old`'s last bar before calling this — it may
    have been written while the bar was still forming (e.g. a live 5m bar
    at t+2min). `new` is authoritative for any timestamp present in both.
    """
    if old.empty:
        return new
    if new.empty:
        return old
    merged = pd.concat([old, new])
    # `new` comes last, so keep="last" makes new win on duplicate timestamps.
    merged = merged[~merged.index.duplicated(keep="last")]
    merged = merged.sort_index()
    return merged


class BarCache:
    """Stores a single parquet file per (ticker, interval) pair."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def _file(self, ticker: str, interval: str) -> Path:
        safe = ticker.replace("/", "_").replace(":", "_")
        return self.path / f"{safe}__{interval}.parquet"

    def get(self, ticker: str, interval: str) -> pd.DataFrame | None:
        fp = self._file(ticker, interval)
        if not fp.exists():
            return None
        try:
            df = pd.read_parquet(fp)
        except Exception as exc:
            log.warning("Cache read failed for %s/%s: %s", ticker, interval, exc)
            return None
        try:
            return validate_bars(df)
        except ValueError as exc:
            log.warning("Cache frame invalid for %s/%s: %s", ticker, interval, exc)
            return None

    def age_sec(self, ticker: str, interval: str) -> float | None:
        """Seconds since the cache file was last written, or None if absent."""
        fp = self._file(ticker, interval)
        if not fp.exists():
            return None
        return time.time() - fp.stat().st_mtime

    def put(self, ticker: str, interval: str, df: pd.DataFrame) -> None:
        validate_bars(df)
        fp = self._file(ticker, interval)
        tmp = fp.with_suffix(".parquet.tmp")
        df.to_parquet(tmp)
        tmp.replace(fp)


class CachedProvider(MarketDataProvider):
    """Parquet cache in front of a live provider with delta fetching.

    Per-ticker decision tree (inside `_resolve_one`):

      - Cache missing or empty → full fetch, write, serve.
      - Cache present and its last bar is within the interval's freshness
        TTL → no fetch, serve cached (trimmed to lookback).
      - Cache present but stale → fetch a small delta window that covers
        the gap plus one day of overlap, merge (new wins on overlap),
        write back, serve.
    """

    def __init__(
        self,
        wrapped: MarketDataProvider,
        cache: BarCache,
        staleness_ttl_sec: dict[str, float] | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._cache = cache
        self._ttl_overrides = staleness_ttl_sec or {}

    def _ttl(self, interval: str) -> float:
        return _ttl_for(interval, self._ttl_overrides)

    def _trim(self, df: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
        if df.empty:
            return df
        end = df.index[-1]
        start = end - pd.Timedelta(days=lookback_days + 1)
        return df.loc[df.index >= start]

    def _resolve_one(
        self,
        ticker: str,
        interval: str,
        lookback_days: int,
        cached: pd.DataFrame | None,
        now: pd.Timestamp,
    ) -> tuple[pd.DataFrame | None, int | None]:
        """Decide per-ticker action.

        Returns (served_frame, fetch_days):
          - (frame, None) — serve this frame from cache, no fetch.
          - (None, n)     — fetch `n` days from wrapped provider.

        Freshness is measured by the cache file's write time, not the
        bar's own timestamp — for daily bars especially, the last bar
        being "24 hours old" says nothing about whether new data exists.
        """
        if cached is None or cached.empty:
            return None, lookback_days  # full fetch

        # Left-edge coverage: does the cache reach back to the requested
        # window start? A pure bar-count check (`len(cached) >= lookback_days`)
        # is misleading for intraday — a 30-day 5m window has ~2,300 bars,
        # so the count test passes even when only 1 day is cached. We
        # compare timestamps instead so a request whose lookback grew
        # (e.g. 30d → 90d) gets the older portion backfilled rather than
        # silently truncated to whatever the cache happened to hold. The
        # `_LEFT_EDGE_SLACK` keeps weekends/holidays from re-triggering a
        # full 30-day refetch on every scan — see the constant's comment.
        wanted_start = now - pd.Timedelta(days=lookback_days)
        if cached.index[0] > wanted_start + _LEFT_EDGE_SLACK:
            return None, lookback_days  # full fetch — backfill left edge

        age = self._cache.age_sec(ticker, interval)
        fresh = age is not None and age < self._ttl(interval)
        if fresh:
            return cached, None
        last_ts = cached.index[-1]
        return None, _delta_lookback_days(last_ts, now)

    def _merge_and_cache(
        self,
        ticker: str,
        interval: str,
        cached: pd.DataFrame | None,
        fresh: pd.DataFrame,
    ) -> pd.DataFrame:
        if cached is None or cached.empty:
            merged = fresh
        else:
            # Drop the cached last bar — it may have been written partial.
            trimmed_old = cached.iloc[:-1] if len(cached) > 1 else cached.iloc[0:0]
            merged = _merge_bars(trimmed_old, fresh)
        self._cache.put(ticker, interval, merged)
        return merged

    def get_bars(self, ticker: str, interval: str, lookback_days: int) -> pd.DataFrame:
        now = pd.Timestamp.now(tz="UTC")
        cached = self._cache.get(ticker, interval)
        served, fetch_days = self._resolve_one(ticker, interval, lookback_days, cached, now)
        if served is not None:
            return self._trim(served, lookback_days)
        assert fetch_days is not None
        from scanner.data.ib_provider import PacingBudgetExhausted
        try:
            fresh = self._wrapped.get_bars(ticker, interval, fetch_days)
        except PacingBudgetExhausted:
            if cached is not None and not cached.empty:
                log.info("Serving stale cache for %s (pacing budget full)", ticker)
                return self._trim(cached, lookback_days)
            raise
        merged = self._merge_and_cache(ticker, interval, cached, fresh)
        return self._trim(merged, lookback_days)

    def disconnect(self) -> None:
        """Forward shutdown to the wrapped provider if it has one.

        Lets the server's shutdown hook reach the IBProvider inside the
        cache wrapper without knowing how the stack is composed.
        """
        inner_dc = getattr(self._wrapped, "disconnect", None)
        if callable(inner_dc):
            inner_dc()

    def get_bars_range(
        self,
        ticker: str,
        interval: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Pass-through to the wrapped provider's date-ranged fetch.

        The parquet cache is a from-today rolling window — it doesn't help
        with arbitrary historical page-back requests (the chart's lazy-load).
        Caching specific [start, end] windows would create a fragmented
        cache layout that's complex to merge with the rolling one. Until
        we have evidence it matters, just forward.

        Raises AttributeError if the wrapped provider doesn't implement
        get_bars_range — only PolygonProvider does today.
        """
        fn = getattr(self._wrapped, "get_bars_range", None)
        if fn is None:
            raise AttributeError(
                f"{type(self._wrapped).__name__} does not support "
                "get_bars_range; only Polygon does"
            )
        return fn(ticker, interval, start, end)

    def get_bars_batch(
        self, tickers: list[str], interval: str, lookback_days: int
    ) -> dict[str, pd.DataFrame]:
        now = pd.Timestamp.now(tz="UTC")
        out: dict[str, pd.DataFrame] = {}
        cached_map: dict[str, pd.DataFrame | None] = {}
        fetch_plans: dict[str, int] = {}

        for t in tickers:
            cached = self._cache.get(t, interval)
            cached_map[t] = cached
            served, fetch_days = self._resolve_one(t, interval, lookback_days, cached, now)
            if served is not None:
                out[t] = self._trim(served, lookback_days)
            else:
                assert fetch_days is not None
                fetch_plans[t] = fetch_days

        if not fetch_plans:
            log.info(
                "Cache fully served %d/%d tickers for %s (no wrapped fetch)",
                len(out), len(tickers), interval,
            )
            return out

        # Group by fetch-days so wrapped provider is called at most once per
        # distinct window size. In practice this is at most two groups —
        # "full history" for cold tickers and "small delta" for warm ones.
        by_days: dict[int, list[str]] = {}
        for t, d in fetch_plans.items():
            by_days.setdefault(d, []).append(t)

        log.info(
            "Cache served %d/%d tickers; fetching %d via wrapped in %d group(s)",
            len(out), len(tickers), len(fetch_plans), len(by_days),
        )

        # Lazy import avoids pulling ib_async in when the server is run
        # with a non-IB provider (synthetic / yfinance).
        from scanner.data.ib_provider import PacingBudgetExhausted

        fetched: dict[str, pd.DataFrame] = {}
        pacing_exc: PacingBudgetExhausted | None = None
        for d, tlist in by_days.items():
            try:
                fetched.update(self._wrapped.get_bars_batch(tlist, interval, d))
            except PacingBudgetExhausted as exc:
                # Upstream pacing is full. Fall back to whatever cache we
                # have for the rest of the batch rather than failing the
                # whole scan — the user would rather see slightly-old
                # scores than a red error banner.
                log.warning(
                    "IB pacing exhausted during delta fetch: %s — "
                    "falling back to cached data for remaining tickers",
                    exc,
                )
                pacing_exc = exc
                break

        for t in fetch_plans:
            fresh = fetched.get(t)
            if fresh is None or fresh.empty:
                cached = cached_map[t]
                if cached is not None and not cached.empty:
                    if pacing_exc is not None:
                        log.info("Serving stale cache for %s (pacing budget full)", t)
                    else:
                        log.warning(
                            "Delta fetch empty for %s — serving stale cache", t,
                        )
                    out[t] = self._trim(cached, lookback_days)
                continue
            merged = self._merge_and_cache(t, interval, cached_map[t], fresh)
            out[t] = self._trim(merged, lookback_days)

        # If pacing blocked us AND nothing could be served from cache, let
        # the server see the exception so it returns a clean 429 instead
        # of an empty scan.
        if pacing_exc is not None and not out:
            raise pacing_exc
        return out
