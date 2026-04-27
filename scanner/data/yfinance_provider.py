"""yfinance-backed MarketDataProvider with batched download + per-ticker fallback."""

from __future__ import annotations

import logging
import time
from typing import cast

import pandas as pd
import yfinance as yf

from scanner.data.base import OHLCV_COLUMNS, MarketDataProvider, validate_bars

log = logging.getLogger(__name__)

_INTERVAL_PERIOD_CAP = {
    "1m": 7,
    "5m": 60,
    "15m": 60,
    "1h": 730,
    "1d": 3650,
}

_FALLBACK_SLEEP_SEC = 0.2


def _normalize_frame(df: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None

    # yfinance sometimes returns MultiIndex columns when multiple tickers are requested.
    if isinstance(df.columns, pd.MultiIndex):
        try:
            df = cast(pd.DataFrame, df.xs(ticker, axis=1, level=-1))
        except KeyError:
            try:
                df = cast(pd.DataFrame, df.xs(ticker, axis=1, level=0))
            except KeyError:
                log.warning("yfinance MultiIndex missing ticker %s", ticker)
                return None

    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    keep = [c for c in OHLCV_COLUMNS if c in df.columns]
    if len(keep) < 5:
        log.warning("yfinance missing OHLCV columns for %s: have %s", ticker, list(df.columns))
        return None

    df = df[list(OHLCV_COLUMNS)].dropna()
    if df.empty:
        return None

    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


class YFinanceProvider(MarketDataProvider):
    """Download OHLCV via yfinance with batched+fallback strategy."""

    def _period_for(self, interval: str, lookback_days: int) -> str:
        cap = _INTERVAL_PERIOD_CAP.get(interval, 3650)
        days = min(lookback_days, cap)
        return f"{days}d"

    def get_bars(self, ticker: str, interval: str, lookback_days: int) -> pd.DataFrame:
        period = self._period_for(interval, lookback_days)
        try:
            raw = yf.download(
                ticker,
                period=period,
                interval=interval,
                auto_adjust=False,
                progress=False,
                threads=False,
            )
        except Exception:
            log.exception("yfinance single-ticker download failed for %s", ticker)
            raise

        df = _normalize_frame(raw, ticker)
        if df is None:
            raise ValueError(f"yfinance returned no data for {ticker}")
        return validate_bars(df)

    def get_bars_batch(
        self, tickers: list[str], interval: str, lookback_days: int
    ) -> dict[str, pd.DataFrame]:
        if not tickers:
            return {}

        period = self._period_for(interval, lookback_days)
        out: dict[str, pd.DataFrame] = {}

        try:
            raw = yf.download(
                " ".join(tickers),
                period=period,
                interval=interval,
                auto_adjust=False,
                progress=False,
                group_by="ticker",
                threads=True,
            )
        except Exception:
            log.exception("yfinance batched download failed; falling back per-ticker")
            raw = None

        if raw is not None and not raw.empty:
            for ticker in tickers:
                df = _normalize_frame(raw, ticker)
                if df is None:
                    continue
                try:
                    out[ticker] = validate_bars(df)
                except ValueError as exc:
                    log.warning("yfinance batched frame invalid for %s: %s", ticker, exc)

        # Fallback for anything missing.
        missing = [t for t in tickers if t not in out]
        for ticker in missing:
            try:
                out[ticker] = self.get_bars(ticker, interval, lookback_days)
            except Exception as exc:
                log.warning("yfinance fallback failed for %s: %s", ticker, exc)
            time.sleep(_FALLBACK_SLEEP_SEC)

        return out
