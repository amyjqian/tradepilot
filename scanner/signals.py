"""Individual bullish signal evaluators.

Each signal takes an enriched DataFrame (see `scanner.indicators.enrich`) plus a
`SignalThresholds` config and returns a strength in [0, 1] for the **latest bar
only**. Signals are pure — no hidden state, deterministic given the input.
"""

from __future__ import annotations

import math

import pandas as pd

from scanner.config import SignalThresholds


def _clip01(value: float) -> float:
    if math.isnan(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def relative_volume_signal(df: pd.DataFrame, cfg: SignalThresholds) -> float:
    """Linear from threshold → 3.0× maps to 0 → 1, clipped."""
    rv = df["rel_volume"].iloc[-1]
    if pd.isna(rv):
        return 0.0
    threshold = cfg.min_relative_volume
    if rv <= threshold:
        return 0.0
    span = max(3.0 - threshold, 1e-9)
    return _clip01((rv - threshold) / span)


def momentum_signal(df: pd.DataFrame, cfg: SignalThresholds) -> float:  # noqa: ARG001
    """Linear 0% → +5% close-to-close maps to 0 → 1; negatives clipped to 0."""
    pct = df["pct_change"].iloc[-1]
    if pd.isna(pct) or pct <= 0.0:
        return 0.0
    return _clip01(pct / 5.0)


def trend_alignment_signal(
    df: pd.DataFrame, cfg: SignalThresholds  # noqa: ARG001
) -> tuple[float, dict[str, bool]]:
    """Fraction of the 4 trend conditions satisfied; returns (score, breakdown)."""
    row = df.iloc[-1]
    above_vwap = bool(row["close"] > row["vwap"]) if pd.notna(row["vwap"]) else False
    above_ema9 = bool(row["close"] > row["ema9"]) if pd.notna(row["ema9"]) else False
    ema9_gt_20 = (
        bool(row["ema9"] > row["ema20"])
        if pd.notna(row["ema9"]) and pd.notna(row["ema20"])
        else False
    )
    ema20_gt_50 = (
        bool(row["ema20"] > row["ema50"])
        if pd.notna(row["ema20"]) and pd.notna(row["ema50"])
        else False
    )
    breakdown = {
        "above_vwap": above_vwap,
        "above_ema9": above_ema9,
        "ema9_gt_ema20": ema9_gt_20,
        "ema20_gt_ema50": ema20_gt_50,
    }
    score = sum(breakdown.values()) / 4.0
    return score, breakdown


def rsi_position_signal(df: pd.DataFrame, cfg: SignalThresholds) -> float:
    """Triangular peak at midpoint of [rsi_min, rsi_max].

    - Above rsi_max: partial credit that decays to 0 at RSI=85 (overbought fade).
    - Below rsi_min: 30% credit scaled by rsi/rsi_min.
    - Inside the zone: triangular 0 → 1 → 0 peaking at midpoint.
    """
    rsi = df["rsi14"].iloc[-1]
    if pd.isna(rsi):
        return 0.0
    lo, hi = cfg.rsi_min, cfg.rsi_max
    if lo >= hi:
        return 0.0
    mid = (lo + hi) / 2.0

    if lo <= rsi <= hi:
        if rsi <= mid:
            return _clip01((rsi - lo) / (mid - lo)) if mid > lo else 1.0
        return _clip01((hi - rsi) / (hi - mid)) if hi > mid else 1.0

    if rsi > hi:
        if rsi >= 85.0:
            return 0.0
        return _clip01((85.0 - rsi) / (85.0 - hi)) if hi < 85.0 else 0.0

    # rsi < lo
    if lo <= 0.0:
        return 0.0
    return _clip01(0.3 * (rsi / lo))


def breakout_proximity_signal(df: pd.DataFrame, cfg: SignalThresholds) -> float:  # noqa: ARG001
    """Linear from 0% → 5% distance-from-20d-high maps to 1 → 0, clipped."""
    dist = df["dist_from_high_20"].iloc[-1]
    if pd.isna(dist):
        return 0.0
    if dist <= 0.0:
        return 1.0
    if dist >= 5.0:
        return 0.0
    return _clip01(1.0 - dist / 5.0)


def apply_hard_filters(df: pd.DataFrame, cfg: SignalThresholds) -> bool:
    """Return True iff the latest bar passes all hard gates."""
    row = df.iloc[-1]

    if cfg.require_above_vwap and (pd.isna(row["vwap"]) or row["close"] <= row["vwap"]):
        return False
    if cfg.require_above_ema9 and (pd.isna(row["ema9"]) or row["close"] <= row["ema9"]):
        return False
    if cfg.require_ema9_above_ema20 and (
        pd.isna(row["ema9"]) or pd.isna(row["ema20"]) or row["ema9"] <= row["ema20"]
    ):
        return False

    gap = row["gap_pct"]
    # gap filter only applies when min_gap_pct > 0 and we have a gap reading.
    return not (pd.notna(gap) and gap > cfg.max_gap_pct)
