"""CLI: run a backtest and write output/backtest_report.json + equity_curve.csv."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from scanner.backtest import backtest  # noqa: E402
from scanner.config import ScannerConfig  # noqa: E402
from scanner.data import get_provider  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_backtest")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Walk-forward backtest over scanner signals.")
    ap.add_argument("--provider", choices=["yfinance", "synthetic", "ibkr", "ib"], default="synthetic")
    ap.add_argument("--tickers", type=str, default=None)
    ap.add_argument("--interval", type=str, default=None)
    ap.add_argument("--lookback", type=int, default=None)
    ap.add_argument("--holding-bars", type=int, default=3)
    ap.add_argument("--target-pct", type=float, default=2.0)
    ap.add_argument("--min-history", type=int, default=30)
    ap.add_argument("--out", type=Path, default=Path("output/backtest_report.json"))
    ap.add_argument("--equity-csv", type=Path, default=Path("output/equity_curve.csv"))
    ap.add_argument("--config", type=Path, default=None)
    return ap.parse_args()


def _print_summary(report: dict[str, object]) -> None:
    console = Console()
    table = Table(title="Backtest Summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    metrics = [
        ("n_signals", report["n_signals"]),
        ("n_winners", report["n_winners"]),
        ("hit_rate", f"{float(report['hit_rate']):.2%}"),
        ("avg_return_pct", f"{float(report['avg_return_pct']):+.2f}%"),
        ("median_return_pct", f"{float(report['median_return_pct']):+.2f}%"),
        ("avg_max_return_pct", f"{float(report['avg_max_return_pct']):+.2f}%"),
        ("avg_max_drawdown_pct", f"{float(report['avg_max_drawdown_pct']):+.2f}%"),
        ("profit_factor", f"{float(report['profit_factor']):.2f}"),
        ("expectancy_pct", f"{float(report['expectancy_pct']):+.2f}%"),
    ]
    for k, v in metrics:
        table.add_row(k, str(v))
    console.print(table)


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

    log.info(
        "Fetching %d tickers via %s (%s / %dd) for backtest",
        len(tickers), args.provider, cfg.interval, cfg.lookback_days,
    )
    provider = get_provider(args.provider)
    data = provider.get_bars_batch(tickers, cfg.interval, cfg.lookback_days)
    log.info("Got bars for %d/%d tickers", len(data), len(tickers))

    report = backtest(
        data, cfg,
        holding_bars=args.holding_bars,
        target_pct=args.target_pct,
        min_history=args.min_history,
    )
    payload = report.to_dict()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    log.info("Wrote %s", args.out)

    args.equity_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.equity_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "equity", "n_trades"])
        writer.writeheader()
        for pt in payload["equity_curve"]:
            writer.writerow(pt)
    log.info("Wrote %s", args.equity_csv)

    _print_summary(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
