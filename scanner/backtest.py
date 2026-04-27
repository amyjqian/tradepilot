"""Walk-forward backtester.

For each date `d` in the union of all ticker dates:
  1. Build a snapshot of each ticker's bars with index ≤ d.
  2. Run `scan()` on the snapshot → flagged tickers.
  3. Measure the forward window `[d+1, d+holding_bars]`:
       max_return_pct, min_return_pct, close_return_pct, hit_target.
  4. Aggregate into a `BacktestReport`.

No look-ahead: the scanner never sees bars with index > d. The cross-dataset
equality test in tests/test_backtest.py enforces that invariant.
"""

from __future__ import annotations

import logging
from statistics import median
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from scanner.config import ScannerConfig
from scanner.engine import scan

log = logging.getLogger(__name__)


class TradeOutcome(BaseModel):
    ticker: str
    entry_date: str  # ISO 8601
    entry_price: float
    score: float
    max_return_pct: float
    min_return_pct: float
    close_return_pct: float
    hit_target: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "entry_date": self.entry_date,
            "entry_price": round(self.entry_price, 4),
            "score": round(self.score, 2),
            "max_return_pct": round(self.max_return_pct, 3),
            "min_return_pct": round(self.min_return_pct, 3),
            "close_return_pct": round(self.close_return_pct, 3),
            "hit_target": self.hit_target,
        }


class BacktestReport(BaseModel):
    n_signals: int
    n_winners: int
    hit_rate: float
    avg_return_pct: float
    median_return_pct: float
    avg_max_return_pct: float
    avg_max_drawdown_pct: float
    profit_factor: float
    expectancy_pct: float
    trades: list[TradeOutcome] = Field(default_factory=list)
    equity_curve: list[dict[str, Any]] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_signals": self.n_signals,
            "n_winners": self.n_winners,
            "hit_rate": round(self.hit_rate, 4),
            "avg_return_pct": round(self.avg_return_pct, 3),
            "median_return_pct": round(self.median_return_pct, 3),
            "avg_max_return_pct": round(self.avg_max_return_pct, 3),
            "avg_max_drawdown_pct": round(self.avg_max_drawdown_pct, 3),
            "profit_factor": round(self.profit_factor, 3),
            "expectancy_pct": round(self.expectancy_pct, 3),
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": [
                {**pt, "equity": round(float(pt["equity"]), 4)} for pt in self.equity_curve
            ],
        }


def _aggregate(trades: list[TradeOutcome]) -> dict[str, float | int]:
    if not trades:
        return {
            "n_signals": 0,
            "n_winners": 0,
            "hit_rate": 0.0,
            "avg_return_pct": 0.0,
            "median_return_pct": 0.0,
            "avg_max_return_pct": 0.0,
            "avg_max_drawdown_pct": 0.0,
            "profit_factor": 0.0,
            "expectancy_pct": 0.0,
        }

    closes = [t.close_return_pct for t in trades]
    maxes = [t.max_return_pct for t in trades]
    drawdowns = [t.min_return_pct for t in trades]  # negative values
    winners = [t for t in trades if t.hit_target]

    sum_wins = sum(c for c in closes if c > 0)
    sum_losses_abs = sum(abs(c) for c in closes if c < 0)
    pf: float
    if sum_losses_abs > 0:
        pf = sum_wins / sum_losses_abs
    elif sum_wins > 0:
        pf = float("inf")
    else:
        pf = 0.0  # noqa: SIM108 — clearer as three branches

    return {
        "n_signals": len(trades),
        "n_winners": len(winners),
        "hit_rate": len(winners) / len(trades),
        "avg_return_pct": sum(closes) / len(closes),
        "median_return_pct": float(median(closes)),
        "avg_max_return_pct": sum(maxes) / len(maxes),
        "avg_max_drawdown_pct": sum(drawdowns) / len(drawdowns),
        "profit_factor": float(pf),
        "expectancy_pct": sum(closes) / len(closes),
    }


def _equity_curve(trades: list[TradeOutcome]) -> list[dict[str, Any]]:
    """One point per trade date, cumulative close_return sum."""
    by_date: dict[str, list[float]] = {}
    for t in trades:
        by_date.setdefault(t.entry_date, []).append(t.close_return_pct)
    points: list[dict[str, Any]] = []
    cum = 0.0
    for date in sorted(by_date):
        day_return = sum(by_date[date]) / len(by_date[date])
        cum += day_return
        points.append({"date": date, "equity": cum, "n_trades": len(by_date[date])})
    return points


def backtest(
    data: dict[str, pd.DataFrame],
    cfg: ScannerConfig,
    holding_bars: int = 3,
    target_pct: float = 2.0,
    min_history: int = 30,
) -> BacktestReport:
    """Walk-forward replay; see module docstring."""
    if not data:
        return BacktestReport.model_validate(_aggregate([]) | {"trades": [], "equity_curve": []})

    # Union of all dates, sorted.
    all_dates: pd.DatetimeIndex = pd.DatetimeIndex(sorted({d for df in data.values() for d in df.index}))
    if len(all_dates) < min_history + holding_bars + 1:
        return BacktestReport.model_validate(_aggregate([]) | {"trades": [], "equity_curve": []})

    trades: list[TradeOutcome] = []

    for i in range(min_history, len(all_dates) - holding_bars):
        d = all_dates[i]
        # Snapshot: each ticker's bars with index <= d.
        snapshot: dict[str, pd.DataFrame] = {}
        for ticker, df in data.items():
            sub = df.loc[df.index <= d]
            if len(sub) >= 25:
                snapshot[ticker] = sub

        if not snapshot:
            continue

        try:
            flagged = scan(snapshot, cfg)
        except Exception:
            log.exception("scan() raised at date %s; skipping", d)
            continue

        for res in flagged:
            ticker_df = data[res.ticker]
            future = ticker_df.loc[ticker_df.index > d]
            window = future.head(holding_bars)
            if len(window) < holding_bars:
                continue

            entry = res.price
            if entry <= 0:
                continue
            max_ret = (float(window["high"].max()) - entry) / entry * 100.0
            min_ret = (float(window["low"].min()) - entry) / entry * 100.0
            close_ret = (float(window["close"].iloc[-1]) - entry) / entry * 100.0

            trades.append(
                TradeOutcome(
                    ticker=res.ticker,
                    entry_date=pd.Timestamp(d).isoformat(),
                    entry_price=entry,
                    score=res.score,
                    max_return_pct=max_ret,
                    min_return_pct=min_ret,
                    close_return_pct=close_ret,
                    hit_target=max_ret >= target_pct,
                )
            )

    agg = _aggregate(trades)
    curve = _equity_curve(trades)
    return BacktestReport(**agg, trades=trades, equity_curve=curve)
