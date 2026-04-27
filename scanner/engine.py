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


def passes_universe_filter(df: pd.DataFrame, cfg: UniverseConfig) -> bool:
    """Apply the universe (price/volume/dollar-volume) filter to the latest bar."""
    row = df.iloc[-1]
    price = float(row["close"])
    if not (cfg.min_price <= price <= cfg.max_price):
        return False
    # 20-day average volume.
    avg_vol_window = df["volume"].tail(20)
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


def scan(data: dict[str, pd.DataFrame], cfg: ScannerConfig) -> list[ScanResult]:
    """Run the scanner pipeline over a dict of {ticker: OHLCV frame}."""
    results: list[ScanResult] = []

    for ticker, df in data.items():
        if df is None or len(df) < MIN_BARS_REQUIRED:
            continue
        enriched = enrich(df)
        if not passes_universe_filter(enriched, cfg.universe):
            continue
        if not apply_hard_filters(enriched, cfg.thresholds):
            continue

        strengths = {name: float(fn(enriched, cfg.thresholds)) for name, fn in SIGNALS.items()}
        score = _weighted_sum(strengths, cfg.weights) * 100.0
        results.append(_to_result(ticker, enriched, cfg, strengths, score))

    results.sort(key=lambda r: r.score, reverse=True)
    return results[: cfg.top_n_results]
