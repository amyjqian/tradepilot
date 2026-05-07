"""Build `SymbolState` instances from a `MarketDataProvider`.

The runner is data-source agnostic; this module is the bridge to the
existing pandas-frame-based providers. Given a provider + tickers +
session date, it pulls daily bars for ATR% / ADV / prior_close, pulls 1m
bars to seed today's session and to build a 20-day TOD volume profile,
and returns ready-to-score states.

Used both by the one-shot `/scoring/scan` endpoint and (eventually) by
the live runner to bootstrap state at session start.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

from scanner.data import MarketDataProvider

from .indicators import atr_pct
from .state import ProfileKind, ProfileLookup as ProfileLookupFn, SymbolState, SymbolStaticContext
from .types import Bar

ET = ZoneInfo("America/New_York")
RTH_OPEN_MIN = 9 * 60 + 30  # 09:30 ET
RTH_CLOSE_MIN = 16 * 60  # 16:00 ET


def _df_to_bars(df: pd.DataFrame) -> list[Bar]:
    """Convert an OHLCV DataFrame to a list of `Bar`. Index is tz-aware UTC.

    Vectorized: avoids per-row pandas overhead (`df.iterrows()` is the
    classic slow pattern). For a 50-ticker scan with 5 days of 1m bars,
    this drops the conversion from ~600ms/ticker → ~30ms/ticker.

    Precision normalization: Polygon returns `datetime64[ms, UTC]` (ms
    precision); other providers may use `[ns]` or `[us]`. We force-cast
    to `[ms]` before `astype('int64')` so the resulting integers are
    always ms-since-epoch regardless of source precision. Without this
    a ms-precision index produces near-zero values after a divide
    intended for ns-precision (the symptom: bars timestamped at
    1969-12-31).
    """
    if df.empty:
        return []
    # Convert to UTC, drop the tz (pandas refuses tz-aware → naive
    # astype), force ms precision, then int64. Resulting ints are
    # ms-since-epoch regardless of the source dtype's precision.
    ts_ms_index = (
        df.index.tz_convert("UTC")
        .tz_localize(None)
        .astype("datetime64[ms]")
    )
    ts_ms = ts_ms_index.astype("int64").tolist()
    o = df["open"].astype(float).tolist()
    h = df["high"].astype(float).tolist()
    lo = df["low"].astype(float).tolist()
    c = df["close"].astype(float).tolist()
    v = df["volume"].astype(float).tolist()
    return [
        Bar(ts_ms=t, open=oo, high=hh, low=ll, close=cc, volume=vv)
        for t, oo, hh, ll, cc, vv in zip(ts_ms, o, h, lo, c, v)
    ]


def session_start_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).timestamp() * 1000)


def _et_minute_of_day(ms: int) -> int:
    dt = datetime.fromtimestamp(ms / 1000.0, tz=ET)
    return dt.hour * 60 + dt.minute


def _build_profile_lookup(
    bars_1m: Sequence[Bar],
    today_session_ms: int,
) -> tuple[dict[int, float], dict[int, float]]:
    """From historical 1m bars (excluding today), build two TOD lookups:

      - cumulative-$-volume from session open to minute-of-day M (avg over days)
      - rolling 30-min $-volume ending at M (avg over days)

    Vectorized through ET-tz pandas conversion (one call instead of
    per-bar `datetime.fromtimestamp`) — this used to dominate the scan
    runtime on watchlists with 21d × 390 bars per symbol.
    """
    if not bars_1m:
        return {}, {}

    # Filter out today's bars (they don't contribute to "historical typical").
    historical = [b for b in bars_1m if b.ts_ms < today_session_ms]
    if not historical:
        return {}, {}

    ts_index = pd.to_datetime(
        [b.ts_ms for b in historical], unit="ms", utc=True
    ).tz_convert(ET)
    minute_of_day = ts_index.hour * 60 + ts_index.minute
    trading_date = ts_index.date  # numpy array of date objects
    dvol = pd.Series(
        [b.close * b.volume for b in historical],
        index=ts_index,
    )

    df = pd.DataFrame(
        {"dvol": dvol.values, "mod": minute_of_day, "date": trading_date},
        index=ts_index,
    )
    in_rth = (df["mod"] >= RTH_OPEN_MIN) & (df["mod"] <= RTH_CLOSE_MIN)
    df = df.loc[in_rth].sort_index()
    if df.empty:
        return {}, {}

    # Per-day cumulative + rolling-30-min within each day.
    df["cum"] = df.groupby("date")["dvol"].cumsum()
    df["rolling30"] = (
        df.groupby("date")["dvol"]
        .rolling(window=30, min_periods=1)
        .sum()
        .reset_index(level=0, drop=True)
    )

    # Average across days at each minute-of-day.
    cum_lookup = df.groupby("mod")["cum"].mean().to_dict()
    rolling_lookup = df.groupby("mod")["rolling30"].mean().to_dict()
    return {int(k): float(v) for k, v in cum_lookup.items()}, {
        int(k): float(v) for k, v in rolling_lookup.items()
    }


def _make_profile_lookup(
    cum_lookup: dict[int, float],
    rolling_lookup: dict[int, float],
    session_start_ms_v: int,
) -> "ProfileLookupFn":
    def lookup(minute_offset: int, kind: ProfileKind) -> float:
        # `minute_offset` is minutes since session_start; convert to
        # ET minute-of-day (session_start is 09:30 = 570).
        mod = RTH_OPEN_MIN + int(minute_offset)
        d = cum_lookup if kind == "cum" else rolling_lookup
        if not d:
            return 0.0
        if mod in d:
            return d[mod]
        # Nearest-minute fallback.
        nearest = min(d.keys(), key=lambda m: abs(m - mod))
        return d[nearest]

    return lookup


def build_state(
    provider: MarketDataProvider,
    ticker: str,
    *,
    today: date,
    daily_lookback_days: int = 30,
    intraday_lookback_days: int = 5,
) -> SymbolState | None:
    """Build a fully-seeded `SymbolState` for `ticker` using historical data.

    Returns `None` if the provider has no data for the ticker. Prefer
    `build_states()` for multiple tickers — it batches REST calls.
    """
    daily_df = provider.get_bars(ticker, "1d", daily_lookback_days)
    one_min_df = provider.get_bars(ticker, "1m", intraday_lookback_days)
    if daily_df.empty or one_min_df.empty:
        return None
    return _state_from_frames(
        ticker=ticker,
        daily_df=daily_df,
        one_min_df=one_min_df,
        today=today,
    )


def _state_from_frames(
    *,
    ticker: str,
    daily_df: pd.DataFrame,
    one_min_df: pd.DataFrame,
    today: date,
) -> SymbolState | None:
    """Shared core between `build_state` and `build_states` (single vs batch)."""
    daily_bars = _df_to_bars(daily_df)
    all_1m_bars = _df_to_bars(one_min_df)

    today_ms = session_start_ms(today)

    # prior_session_close, yesterday H/L from the most recent prior daily bar.
    prior_daily = [b for b in daily_bars if datetime.fromtimestamp(b.ts_ms / 1000.0, tz=ET).date() < today]
    if not prior_daily:
        return None
    prior = prior_daily[-1]

    # 20-day ADV $: mean of (close * volume) over the last 20 prior dailies.
    last20 = prior_daily[-20:]
    adv_dollar = sum(b.close * b.volume for b in last20) / max(1, len(last20))

    # 20-day ATR%: 14-period ATR% on prior dailies, take the last value.
    atr_pct_series = atr_pct(prior_daily, 14)
    atr_pct_20d = next(
        (v for v in reversed(atr_pct_series) if v is not None),
        0.0,
    )

    cum_lookup, rolling_lookup = _build_profile_lookup(all_1m_bars, today_ms)
    profile_lookup = _make_profile_lookup(cum_lookup, rolling_lookup, today_ms)

    static = SymbolStaticContext(
        symbol=ticker,
        session_start_ms=today_ms,
        daily_bars=prior_daily[-30:],
        yesterday_high=prior.high,
        yesterday_low=prior.low,
        prior_session_close=prior.close,
        adv_dollar=adv_dollar,
        atr_pct_20d=atr_pct_20d or 0.0,
        avg_spread_pct=0.04,  # TODO: derive from quotes; placeholder
        rolling_avg_spread_pct=0.04,
        profile_lookup=profile_lookup,
    )
    state = SymbolState(static=static)

    # Seed bars_1m/5m/15m with rolling multi-day history. Prior days are
    # aggregated through a per-day temp aggregator inside the helper;
    # today's bars flow through on_minute_bar so today_* metadata is set.
    # This is the bar-scope correction (PER_15_MINUTE_SCORING_PATCH.md):
    # EMAs and RSI need rolling history to be valid from session open;
    # without it, 15m EMA21 wouldn't seed until ~14:45 ET.
    state.populate_rolling_history(all_1m_bars)

    return state


def build_states(
    provider: MarketDataProvider,
    tickers: Sequence[str],
    *,
    today: date | None = None,
    daily_lookback_days: int = 30,
    intraday_lookback_days: int = 5,
    eval_at_ms: int | None = None,
) -> dict[str, SymbolState]:
    """Build states for many tickers; missing data → omitted (not raised).

    Uses `provider.get_bars_batch` for both daily and 1m fetches so the
    backend can fan them out (Polygon's grouped-daily endpoint is one
    HTTP request; cached providers similarly hit disk in one pass). The
    earlier per-ticker loop did 2N sequential HTTP calls and dominated
    the scan's wall-clock time on watchlists of 50+ symbols.

    `eval_at_ms` enables the cache "complete-through" shortcut: when
    set, tickers whose on-disk cache already extends to that timestamp
    are served from cache without TTL freshness checks (no Polygon
    delta fetch). The engine truncates to `eval_at_ms` anyway so
    fresher bars wouldn't be used. Callers in verify mode pass this so
    repeated runs against the same past timestamp don't keep poking
    Polygon for deltas the engine discards.
    """
    if today is None:
        today = datetime.now(tz=ET).date()
    upper = [t.upper() for t in tickers]
    log.info(
        "scoring.build_states start: %d tickers, daily=%dd, intraday=%dd, today=%s%s",
        len(upper), daily_lookback_days, intraday_lookback_days, today.isoformat(),
        f", eval_at_ms={eval_at_ms}" if eval_at_ms else "",
    )

    # When verify mode supplies eval_at_ms, prefer the cache-only path
    # (CachedProvider's `get_bars_batch_complete_through`) which skips
    # delta fetches for any ticker whose cache already covers the eval
    # timestamp. Falls back to normal get_bars_batch for tickers that
    # don't yet have sufficient coverage. The duck-typed dispatch keeps
    # non-cached providers (synthetic, raw Polygon) working unchanged.
    complete_through_ts = (
        pd.Timestamp(eval_at_ms, unit="ms", tz="UTC")
        if eval_at_ms is not None
        else None
    )
    fetch_complete: Any = getattr(
        provider, "get_bars_batch_complete_through", None
    )

    def _fetch_batch(interval: str, lookback: int) -> dict[str, pd.DataFrame]:
        if complete_through_ts is not None and fetch_complete is not None:
            return fetch_complete(upper, interval, lookback, complete_through_ts)  # type: ignore[no-any-return]
        return provider.get_bars_batch(upper, interval, lookback)

    t0 = time.perf_counter()
    try:
        daily_batch = _fetch_batch("1d", daily_lookback_days)
    except Exception:
        log.exception("scoring.build_states: daily batch fetch failed")
        daily_batch = {}
    log.info(
        "scoring.build_states: daily batch fetched %d/%d in %.2fs",
        len(daily_batch), len(upper), time.perf_counter() - t0,
    )

    t1 = time.perf_counter()
    try:
        one_min_batch = _fetch_batch("1m", intraday_lookback_days)
    except Exception:
        log.exception("scoring.build_states: 1m batch fetch failed")
        one_min_batch = {}
    log.info(
        "scoring.build_states: 1m batch fetched %d/%d in %.2fs",
        len(one_min_batch), len(upper), time.perf_counter() - t1,
    )

    t2 = time.perf_counter()
    out: dict[str, SymbolState] = {}
    skipped_no_data: list[str] = []
    skipped_build_failed: list[str] = []

    # Parallelize the per-ticker `_state_from_frames` work across threads.
    # Each ticker's build is independent (no shared mutable state) and the
    # work is mostly numpy/pandas — pandas releases the GIL on its C-level
    # operations, so threading gives a real speedup. 8 workers is a sweet
    # spot for typical 50-ticker watchlists; bumps yield diminishing returns.
    work: list[tuple[str, pd.DataFrame, pd.DataFrame]] = []
    for t in upper:
        daily_df = daily_batch.get(t)
        one_min_df = one_min_batch.get(t)
        if daily_df is None or one_min_df is None or daily_df.empty or one_min_df.empty:
            skipped_no_data.append(t)
            continue
        work.append((t, daily_df, one_min_df))

    if work:
        import concurrent.futures

        def _build_one(item: tuple[str, pd.DataFrame, pd.DataFrame]) -> tuple[
            str, SymbolState | None, BaseException | None,
        ]:
            t, d, m = item
            try:
                return t, _state_from_frames(
                    ticker=t, daily_df=d, one_min_df=m, today=today,  # type: ignore[arg-type]
                ), None
            except BaseException as exc:
                return t, None, exc

        max_workers = min(8, len(work))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="build-state",
        ) as ex:
            for t, state, exc in ex.map(_build_one, work):
                if exc is not None:
                    log.warning(
                        "scoring.build_states: %s build_state raised",
                        t, exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    skipped_build_failed.append(t)
                    continue
                if state is None:
                    skipped_build_failed.append(t)
                    continue
                out[t] = state

    log.info(
        "scoring.build_states done: %d built, %d no-data, %d failed, build phase %.2fs (total %.2fs)",
        len(out), len(skipped_no_data), len(skipped_build_failed),
        time.perf_counter() - t2, time.perf_counter() - t0,
    )
    if skipped_no_data:
        log.info("scoring.build_states no-data tickers (first 10): %s", skipped_no_data[:10])
    return out
