"""Tests for scanner.tuning."""

from __future__ import annotations

import pandas as pd
import pytest

from scanner.config import ScannerConfig
from scanner.data import SyntheticProvider
from scanner.tuning import DEFAULT_GRID, _apply_params, _split_by_date, grid_search


def _data(n: int = 8, lookback: int = 180) -> dict[str, pd.DataFrame]:
    p = SyntheticProvider()
    return {f"T{i:02d}": p.get_bars(f"T{i:02d}", "1d", lookback) for i in range(n)}


def test_apply_params_updates_cfg() -> None:
    cfg = ScannerConfig()
    out = _apply_params(cfg, {"min_relative_volume": 2.0})
    assert out.thresholds.min_relative_volume == 2.0
    # Original unchanged.
    assert cfg.thresholds.min_relative_volume != 2.0 or cfg is not out


def test_apply_params_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown threshold param"):
        _apply_params(ScannerConfig(), {"not_a_param": 1.0})


def test_split_by_date_roughly_7030() -> None:
    d = _data(n=4, lookback=100)
    train, test = _split_by_date(d, train_frac=0.7)
    for t in train.values():
        assert len(t) > 0
    for t in test.values():
        assert len(t) > 0
    # Train should be roughly 70% of bars.
    train_bars = sum(len(v) for v in train.values())
    total_bars = sum(len(v) for v in d.values())
    assert 0.6 <= train_bars / total_bars <= 0.8


def test_grid_search_returns_rows_sorted() -> None:
    d = _data(n=6, lookback=200)
    grid = {"min_relative_volume": [1.2, 2.0], "rsi_min": [50.0]}
    rows = grid_search(d, grid, metric="hit_rate", holding_bars=3, target_pct=2.0)
    assert len(rows) == 2
    # Sorted descending on is_hit_rate.
    assert rows[0]["is_hit_rate"] >= rows[1]["is_hit_rate"]
    for row in rows:
        assert "params" in row
        assert "is_profit_factor" in row
        assert "oos_profit_factor" in row


def test_default_grid_exposed() -> None:
    assert "min_relative_volume" in DEFAULT_GRID
    assert "rsi_min" in DEFAULT_GRID
    assert "rsi_max" in DEFAULT_GRID
    assert "breakout_proximity_pct" in DEFAULT_GRID
