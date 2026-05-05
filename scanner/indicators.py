"""Pure, vectorized technical indicators.

All functions are deterministic: same input → same output. No hidden clock or
random state. `enrich(df)` appends the canonical column set used everywhere
downstream (engine, signals, dashboard).
"""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI clipped to [0, 100]."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder's smoothing ≡ EMA with alpha = 1/period.
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    result = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss is 0 the series should read 100 (all gains); when avg_gain is
    # also 0 the RSI is undefined — treat as 50 (neutral) rather than NaN so
    # downstream signals never see NaN on fully-flat stretches.
    result = result.where(avg_loss != 0.0, 100.0)
    result = result.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    return result.astype(float).clip(0.0, 100.0)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session-reset VWAP.

    Daily bars → cumulative-from-series-start (the common dashboard convention).
    Intraday bars → reset per calendar date via groupby.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].astype(float)
    pv = typical * vol

    if isinstance(df.index, pd.DatetimeIndex) and _is_intraday_index(df.index):
        dates = df.index.tz_convert("UTC").normalize() if df.index.tz else df.index.normalize()
        cum_pv = pv.groupby(dates).cumsum()
        cum_vol = vol.groupby(dates).cumsum()
    else:
        cum_pv = pv.cumsum()
        cum_vol = vol.cumsum()

    result = cum_pv / cum_vol.replace(0.0, pd.NA)
    return result.astype(float)


def _is_intraday_index(idx: pd.Index) -> bool:
    if not isinstance(idx, pd.DatetimeIndex) or len(idx) < 2:
        return False
    delta = idx[1] - idx[0]
    return delta < pd.Timedelta(days=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def relative_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    avg = df["volume"].rolling(window=period, min_periods=period).mean()
    return (df["volume"] / avg.replace(0.0, pd.NA)).astype(float)


def gap_pct(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return ((df["open"] - prev_close) / prev_close * 100.0).astype(float)


def pct_change_today(df: pd.DataFrame) -> pd.Series:
    """Percent change versus prior session close — the standard daily quote.

    Intraday bars → each bar reads `(close - prior_session_close) / prior_session_close`,
    where `prior_session_close` is the last bar of the previous calendar date
    in the frame. This includes the gap from yesterday's close, which is what
    a quote screen and a day trader mean by "today's % change" (e.g. MU at
    $585 vs $542 prior close = +8%). Bars in the first session of the lookback
    window have no prior session to anchor against; they fall back to their
    own session's first-bar open so the value is still relative to today.
    Daily bars → `close.pct_change()` (vs prior bar's close — same idea).
    """
    if isinstance(df.index, pd.DatetimeIndex) and _is_intraday_index(df.index):
        dates = (
            df.index.tz_convert("UTC").normalize()
            if df.index.tz is not None
            else df.index.normalize()
        )
        last_close_by_date = df["close"].groupby(dates).last()
        prior_close_by_date = last_close_by_date.shift(1).to_dict()
        anchor = pd.Series(
            [prior_close_by_date.get(d) for d in dates],
            index=df.index,
            dtype="float64",
        )
        # First session in the lookback has no prior close — fall back to that
        # session's first-bar open so the value is still anchored to today
        # rather than NaN (which downstream code would silently coerce to 0).
        first_open = df["open"].groupby(dates).transform("first")
        anchor = anchor.where(anchor.notna(), first_open)
        return ((df["close"] - anchor) / anchor * 100.0).astype(float)
    return (df["close"].pct_change() * 100.0).astype(float)


def distance_from_high(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Percent below the rolling `period`-bar high (always ≥ 0)."""
    rolling_high = df["high"].rolling(window=period, min_periods=period).max()
    return ((rolling_high - df["close"]) / rolling_high * 100.0).astype(float)


def bollinger_bands(
    close: pd.Series, period: int = 20, n_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, period)
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    lower = mid - n_std * std
    upper = mid + n_std * std
    return lower, mid, upper


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Append canonical indicator columns without dropping any rows."""
    out = df.copy()
    close = out["close"]
    out["ema9"] = ema(close, 9)
    out["ema20"] = ema(close, 20)
    out["ema50"] = ema(close, 50)
    out["rsi14"] = rsi(close, 14)
    out["vwap"] = vwap(out)
    out["atr14"] = atr(out, 14)
    out["rel_volume"] = relative_volume(out, 20)
    out["gap_pct"] = gap_pct(out)
    out["pct_change"] = pct_change_today(out)
    out["dist_from_high_20"] = distance_from_high(out, 20)
    lower, mid, upper = bollinger_bands(close, 20, 2.0)
    out["bb_lower"] = lower
    out["bb_mid"] = mid
    out["bb_upper"] = upper
    return out


ENRICHED_COLUMNS: tuple[str, ...] = (
    "ema9",
    "ema20",
    "ema50",
    "rsi14",
    "vwap",
    "atr14",
    "rel_volume",
    "gap_pct",
    "pct_change",
    "dist_from_high_20",
    "bb_lower",
    "bb_mid",
    "bb_upper",
)
