"""Scanner orchestrator.

`scan()` takes a dict of per-ticker OHLCV frames and returns the top-N ranked
`ScanResult`s. All signal logic is pure & backtestable: given identical inputs,
it returns identical outputs.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pandas as pd
from pydantic import BaseModel, Field

from scanner.config import ScannerConfig, ScoringWeights, SignalThresholds, UniverseConfig
from scanner.indicators import enrich
from scanner.signals import (
    apply_hard_filters,
    breakout_proximity_signal,
    momentum_signal,
    relative_volume_signal,
    rsi_position_signal,
    trend_alignment_signal,
)

SignalFn = Callable[[pd.DataFrame, SignalThresholds], float]

MIN_BARS_REQUIRED: int = 25

SIGNALS: dict[str, SignalFn] = {
    "relative_volume": relative_volume_signal,
    "momentum": momentum_signal,
    "trend_alignment": lambda df, cfg: trend_alignment_signal(df, cfg)[0],
    "rsi_position": rsi_position_signal,
    "breakout_proximity": breakout_proximity_signal,
}


class ScanResult(BaseModel):
    """One row of the scan output."""

    ticker: str
    score: float
    price: float
    pct_change: float
    rel_volume: float
    rsi: float
    above_vwap: bool
    above_ema9: bool
    ema_stacked: bool
    dist_from_20d_high_pct: float
    signals: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Round numerics for display / JSON emission."""

        def r(v: float, nd: int = 2) -> float:
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return 0.0
            return round(float(v), nd)

        return {
            "ticker": self.ticker,
            "score": r(self.score, 2),
            "price": r(self.price, 2),
            "pct_change": r(self.pct_change, 2),
            "rel_volume": r(self.rel_volume, 2),
            "rsi": r(self.rsi, 2),
            "above_vwap": self.above_vwap,
            "above_ema9": self.above_ema9,
            "ema_stacked": self.ema_stacked,
            "dist_from_20d_high_pct": r(self.dist_from_20d_high_pct, 2),
            "signals": {k: r(v, 3) for k, v in self.signals.items()},
            "reasons": list(self.reasons),
        }


def passes_universe_filter(
    df: pd.DataFrame,
    cfg: UniverseConfig,
    *,
    daily_df: pd.DataFrame | None = None,
) -> bool:
    """Apply price + liquidity gates to the latest bar.

    Liquidity ("is this ticker tradeable?") is a *per-day* property, so we
    always evaluate it on daily bars. When `daily_df` is supplied (intraday
    scans wire this up at the API layer), the 20-bar mean is computed from
    daily volume — the same number a daily scan would produce. When omitted
    (daily-bar scans, where `df` is already daily), we fall back to `df`
    itself. Without this, intraday scans applied a daily-tuned floor of
    500K shares against 20 *minutes* of volume and dropped almost everything.
    """
    row = df.iloc[-1]
    price = float(row["close"])
    if price < cfg.min_price:
        return False

    liquidity_frame = daily_df if daily_df is not None else df
    avg_vol_window = liquidity_frame["volume"].tail(20)
    if len(avg_vol_window) < 20:
        return False
    avg_vol = float(avg_vol_window.mean())
    if avg_vol < cfg.min_avg_volume:
        return False
    return price * avg_vol >= cfg.min_dollar_volume


def _weighted_sum(strengths: dict[str, float], weights: ScoringWeights) -> float:
    w = weights.as_dict()
    return sum(strengths.get(k, 0.0) * w[k] for k in w)


def build_reasons(df: pd.DataFrame, strengths: dict[str, float], cfg: SignalThresholds) -> list[str]:
    """Plain-English bullish factors for the latest bar."""
    row = df.iloc[-1]
    out: list[str] = []

    rv = row.get("rel_volume")
    if pd.notna(rv) and rv >= cfg.min_relative_volume:
        out.append(f"Relative volume {float(rv):.2f}x average")

    pct = row.get("pct_change")
    if pd.notna(pct) and pct >= 1.0:
        out.append(f"Up {float(pct):.2f}% on the day")

    if (
        pd.notna(row.get("ema9"))
        and pd.notna(row.get("ema20"))
        and pd.notna(row.get("ema50"))
        and row["ema9"] > row["ema20"] > row["ema50"]
    ):
        out.append("EMAs stacked bullishly (9>20>50)")

    rsi = row.get("rsi14")
    if pd.notna(rsi) and cfg.rsi_min <= rsi <= cfg.rsi_max:
        out.append(f"RSI {float(rsi):.0f} in bullish zone")

    dist = row.get("dist_from_high_20")
    if (
        pd.notna(dist)
        and strengths.get("breakout_proximity", 0.0) >= 0.8
    ):
        out.append(f"Within {float(dist):.2f}% of 20-day high")

    return out


def _to_result(ticker: str, enriched: pd.DataFrame, cfg: ScannerConfig,
               strengths: dict[str, float], score: float) -> ScanResult:
    row = enriched.iloc[-1]
    _, trend_breakdown = trend_alignment_signal(enriched, cfg.thresholds)
    reasons = build_reasons(enriched, strengths, cfg.thresholds)
    return ScanResult(
        ticker=ticker,
        score=score,
        price=float(row["close"]),
        pct_change=float(row["pct_change"]) if pd.notna(row["pct_change"]) else 0.0,
        rel_volume=float(row["rel_volume"]) if pd.notna(row["rel_volume"]) else 0.0,
        rsi=float(row["rsi14"]) if pd.notna(row["rsi14"]) else 0.0,
        above_vwap=bool(trend_breakdown["above_vwap"]),
        above_ema9=bool(trend_breakdown["above_ema9"]),
        ema_stacked=bool(
            trend_breakdown["ema9_gt_ema20"] and trend_breakdown["ema20_gt_ema50"]
        ),
        dist_from_20d_high_pct=(
            float(row["dist_from_high_20"]) if pd.notna(row["dist_from_high_20"]) else 0.0
        ),
        signals=dict(strengths),
        reasons=reasons,
    )


def _reanchor_pct_change_to_daily_prior(
    enriched: pd.DataFrame, daily_df: pd.DataFrame
) -> None:
    """Override `pct_change` for the most recent session's bars using the prior
    daily-bar close.

    Why: the in-frame fallback in `pct_change_today` only finds a prior session
    when the intraday lookback window contains one. With `lookback_days=2` on a
    Monday, the window holds only today's bars (Sat/Sun have no sessions), so
    every bar's anchor falls back to today's open and the overnight gap is
    hidden. Daily bars always include the prior trading day, so they're the
    reliable source for "yesterday's regular close".
    """
    if not isinstance(enriched.index, pd.DatetimeIndex) or daily_df.empty:
        return
    intraday_dates = (
        enriched.index.tz_convert("UTC").normalize()
        if enriched.index.tz is not None
        else enriched.index.normalize()
    )
    daily_dates = (
        daily_df.index.tz_convert("UTC").normalize()
        if daily_df.index.tz is not None
        else daily_df.index.normalize()
    )
    latest_session = intraday_dates[-1]
    # Strict <: if Polygon emits today's running daily aggregate after the
    # close, we still want the *prior* day, not today.
    prior_closes = daily_df.loc[daily_dates < latest_session, "close"]
    if prior_closes.empty:
        return
    prior_close = float(prior_closes.iloc[-1])
    if prior_close <= 0.0:
        return
    mask = intraday_dates == latest_session
    enriched.loc[mask, "pct_change"] = (
        (enriched.loc[mask, "close"] - prior_close) / prior_close * 100.0
    )


def scan(
    data: dict[str, pd.DataFrame],
    cfg: ScannerConfig,
    *,
    daily_data: dict[str, pd.DataFrame] | None = None,
) -> list[ScanResult]:
    """Run the scanner pipeline over a dict of {ticker: OHLCV frame}.

    `daily_data` (optional) supplies daily bars used for two purposes: (1) the
    liquidity gate in `passes_universe_filter` (computed per-day regardless of
    scan interval) and (2) the prior-close anchor for today's `pct_change` on
    intraday scans whose lookback doesn't include the previous trading day.
    """
    results: list[ScanResult] = []

    for ticker, df in data.items():
        if df is None or len(df) < MIN_BARS_REQUIRED:
            continue
        enriched = enrich(df)
        daily_df = daily_data.get(ticker) if daily_data is not None else None
        if daily_df is not None and not daily_df.empty:
            _reanchor_pct_change_to_daily_prior(enriched, daily_df)
        if not passes_universe_filter(enriched, cfg.universe, daily_df=daily_df):
            continue
        if not apply_hard_filters(enriched, cfg.thresholds):
            continue

        strengths = {name: float(fn(enriched, cfg.thresholds)) for name, fn in SIGNALS.items()}
        score = _weighted_sum(strengths, cfg.weights) * 100.0
        results.append(_to_result(ticker, enriched, cfg, strengths, score))

    results.sort(key=lambda r: r.score, reverse=True)
    return results[: cfg.top_n_results]
