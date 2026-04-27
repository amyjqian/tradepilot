"""CLI: grid-search threshold tuning; writes output/tuning_results.csv."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.config import ScannerConfig  # noqa: E402
from scanner.data import get_provider  # noqa: E402
from scanner.tuning import DEFAULT_GRID, grid_search  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_tuning")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Grid-search scanner thresholds against a backtest metric.")
    ap.add_argument("--provider", choices=["yfinance", "synthetic", "ibkr", "ib"], default="synthetic")
    ap.add_argument("--tickers", type=str, default=None)
    ap.add_argument("--interval", type=str, default=None)
    ap.add_argument("--lookback", type=int, default=None)
    ap.add_argument("--metric", type=str, default="profit_factor")
    ap.add_argument("--holding-bars", type=int, default=3)
    ap.add_argument("--target-pct", type=float, default=2.0)
    ap.add_argument("--grid", type=Path, default=None, help="Optional JSON grid override")
    ap.add_argument("--out", type=Path, default=Path("output/tuning_results.csv"))
    ap.add_argument("--config", type=Path, default=None)
    return ap.parse_args()


def _flatten(row: dict[str, object]) -> dict[str, object]:
    """Flatten {'params': {...}, 'is_foo': x, ...} into a CSV-friendly row."""
    out: dict[str, object] = {}
    params = row.get("params", {})
    if isinstance(params, dict):
        for k, v in params.items():
            out[f"param_{k}"] = v
    for k, v in row.items():
        if k == "params":
            continue
        out[k] = v
    return out


def main() -> int:
    args = parse_args()
    cfg = ScannerConfig.from_json(args.config) if args.config else ScannerConfig()
    if args.interval:
        cfg.interval = args.interval  # type: ignore[assignment]
    if args.lookback:
        cfg.lookback_days = args.lookback

    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else cfg.default_tickers
    )

    if args.grid:
        with args.grid.open("r", encoding="utf-8") as fh:
            grid = json.load(fh)
    else:
        grid = DEFAULT_GRID

    log.info("Fetching %d tickers via %s", len(tickers), args.provider)
    provider = get_provider(args.provider)
    data = provider.get_bars_batch(tickers, cfg.interval, cfg.lookback_days)

    log.info("Running grid search: %d combos", _combo_count(grid))
    rows = grid_search(
        data, grid,
        metric=args.metric,
        holding_bars=args.holding_bars,
        target_pct=args.target_pct,
        base_config=cfg,
    )
    if not rows:
        log.warning("Grid search returned zero rows")
        return 1

    flat = [_flatten(r) for r in rows]
    fieldnames = sorted({k for row in flat for k in row})
    # Stable column order: param_* first, then is_* train, then oos_*.
    param_cols = sorted(c for c in fieldnames if c.startswith("param_"))
    is_cols = sorted(c for c in fieldnames if c.startswith("is_"))
    oos_cols = sorted(c for c in fieldnames if c.startswith("oos_"))
    other = [c for c in fieldnames if c not in param_cols + is_cols + oos_cols]
    ordered = param_cols + is_cols + oos_cols + other

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ordered)
        writer.writeheader()
        for r in flat:
            writer.writerow(r)
    log.info("Wrote %s (%d rows)", args.out, len(flat))
    return 0


def _combo_count(grid: dict[str, list[float]]) -> int:
    n = 1
    for v in grid.values():
        n *= max(1, len(v))
    return n


if __name__ == "__main__":
    raise SystemExit(main())
