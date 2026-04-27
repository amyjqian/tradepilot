"""Scanner configuration — Pydantic models that round-trip through JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Interval = Literal["1d", "1h", "15m", "5m", "1m"]


class UniverseConfig(BaseModel):
    """Filters applied before any signal scoring."""

    min_price: float = 2.0
    max_price: float = 500.0
    min_avg_volume: int = 500_000
    min_dollar_volume: float = 10_000_000


class SignalThresholds(BaseModel):
    """Per-signal thresholds and hard-filter gates."""

    min_relative_volume: float = 1.5
    min_gap_pct: float = 1.0
    max_gap_pct: float = 15.0
    rsi_min: float = 50.0
    rsi_max: float = 70.0
    breakout_proximity_pct: float = 0.5

    require_above_vwap: bool = True
    require_above_ema9: bool = True
    require_ema9_above_ema20: bool = True


class ScoringWeights(BaseModel):
    """Weights applied when combining the five signal strengths.

    Must sum to 1.0 within float tolerance.
    """

    relative_volume: float = 0.30
    momentum: float = 0.25
    trend_alignment: float = 0.20
    rsi_position: float = 0.10
    breakout_proximity: float = 0.15

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> ScoringWeights:
        total = (
            self.relative_volume
            + self.momentum
            + self.trend_alignment
            + self.rsi_position
            + self.breakout_proximity
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"ScoringWeights must sum to 1.0; got {total:.6f}")
        return self

    def as_dict(self) -> dict[str, float]:
        return {
            "relative_volume": self.relative_volume,
            "momentum": self.momentum,
            "trend_alignment": self.trend_alignment,
            "rsi_position": self.rsi_position,
            "breakout_proximity": self.breakout_proximity,
        }


DEFAULT_TICKERS: list[str] = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
    # Semis
    "AMD", "AVGO", "TSM", "MU", "QCOM", "INTC",
    # Financials
    "JPM", "BAC", "GS", "MS", "V", "MA",
    # Consumer
    "WMT", "COST", "HD", "NKE", "MCD", "SBUX",
    # Healthcare
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "TMO",
    # Industrials
    "CAT", "BA", "DE", "GE", "HON",
    # Energy
    "XOM", "CVX", "COP",
    # High-beta / growth
    "TSLA", "NFLX", "CRM", "COIN", "PLTR", "SNOW", "SHOP", "UBER", "ABNB", "ORCL",
    "ADBE",
]


class ScannerConfig(BaseModel):
    """Top-level scanner configuration."""

    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    thresholds: SignalThresholds = Field(default_factory=SignalThresholds)
    weights: ScoringWeights = Field(default_factory=ScoringWeights)

    default_tickers: list[str] = Field(default_factory=lambda: list(DEFAULT_TICKERS))
    top_n_results: int = 20
    interval: Interval = "1d"
    lookback_days: int = 90

    @classmethod
    def from_json(cls, path: str | Path) -> ScannerConfig:
        p = Path(path)
        with p.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return cls.model_validate(payload)

    def to_json(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            fh.write(self.model_dump_json(indent=2))
            fh.write("\n")
