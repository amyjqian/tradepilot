"""Grid-search tuner for signal thresholds.

Walks a Cartesian product of threshold values, runs the backtester on each, and
returns rows sorted by the chosen metric descending. Uses a 70/30 date-based
train/test split and warns loudly when out-of-sample performance is less than
half of in-sample (a cheap overfit alarm).
"""

from __future__ import annotations

import itertools
import logging
import warnings
from copy import deepcopy
from typing import Any

import pandas as pd

from scanner.backtest import BacktestReport, backtest
from scanner.config import ScannerConfig

log = logging.getLogger(__name__)


DEFAULT_GRID: dict[str, list[float]] = {
    "min_relative_volume": [1.2, 1.5, 2.0, 2.5],
    "rsi_min": [45.0, 50.0, 55.0],
    "rsi_max": [65.0, 70.0, 75.0],
    "breakout_proximity_pct": [0.3, 0.5, 1.0],
}


def _apply_params(cfg: ScannerConfig, params: dict[str, float]) -> ScannerConfig:
    out = deepcopy(cfg)
    for k, v in params.items():
        if not hasattr(out.thresholds, k):
            raise ValueError(f"Unknown threshold param: {k}")
        setattr(out.thresholds, k, v)
    return out


def _split_by_date(
    data: dict[str, pd.DataFrame], train_frac: float = 0.7
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    all_dates = sorted({d for df in data.values() for d in df.index})
    if not all_dates:
        return {}, {}
    cut_idx = int(len(all_dates) * train_frac)
    cut = all_dates[cut_idx]
    train = {t: df.loc[df.index <= cut] for t, df in data.items()}
    test = {t: df.loc[df.index > cut] for t, df in data.items()}
    return train, test


def _metrics(report: BacktestReport) -> dict[str, float | int]:
    return {
        "n_signals": report.n_signals,
        "hit_rate": report.hit_rate,
        "avg_return_pct": report.avg_return_pct,
        "profit_factor": report.profit_factor if report.profit_factor != float("inf") else 99.0,
        "expectancy_pct": report.expectancy_pct,
        "avg_max_drawdown_pct": report.avg_max_drawdown_pct,
    }


def grid_search(
    data: dict[str, pd.DataFrame],
    param_grid: dict[str, list[float]] | None = None,
    metric: str = "profit_factor",
    holding_bars: int = 3,
    target_pct: float = 2.0,
    base_config: ScannerConfig | None = None,
) -> list[dict[str, Any]]:
    """Exhaustive grid search. Returns rows sorted by `metric` (IS) desc."""
    grid = param_grid or DEFAULT_GRID
    cfg = base_config or ScannerConfig()

    train, test = _split_by_date(data, train_frac=0.7)
    if not train or not test:
        raise ValueError("Not enough data to split 70/30")

    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    rows: list[dict[str, Any]] = []

    for combo in combos:
        params = dict(zip(keys, combo, strict=True))
        try:
            cfg_variant = _apply_params(cfg, params)
        except ValueError:
            continue

        try:
            is_report = backtest(train, cfg_variant, holding_bars=holding_bars, target_pct=target_pct)
            oos_report = backtest(test, cfg_variant, holding_bars=holding_bars, target_pct=target_pct)
        except Exception:
            log.exception("backtest failed for params %s", params)
            continue

        is_m = _metrics(is_report)
        oos_m = _metrics(oos_report)

        if (
            metric in is_m
            and is_m[metric] > 0
            and metric in oos_m
            and oos_m[metric] < 0.5 * is_m[metric]
        ):
            warnings.warn(
                f"Overfit alarm: {params} IS {metric}={is_m[metric]:.3f} "
                f"OOS {metric}={oos_m[metric]:.3f}",
                UserWarning,
                stacklevel=2,
            )

        row: dict[str, Any] = {"params": params}
        row.update({f"is_{k}": v for k, v in is_m.items()})
        row.update({f"oos_{k}": v for k, v in oos_m.items()})
        rows.append(row)

    key = f"is_{metric}"
    rows.sort(key=lambda r: r.get(key, 0.0), reverse=True)
    return rows
