"""CLI: run a bullish scan and persist the results to output/scan_results.json."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

# Make the project root importable when this script is invoked from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.alerts import build_notifiers  # noqa: E402
from scanner.config import ScannerConfig  # noqa: E402
from scanner.data import get_provider  # noqa: E402
from scanner.engine import scan  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_scan")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Run a bullish stock scan.")
    ap.add_argument("--provider", choices=["yfinance", "synthetic", "ibkr", "ib"], default="synthetic")
    ap.add_argument("--tickers", type=str, default=None, help="Comma-separated ticker list")
    ap.add_argument("--interval", type=str, default=None)
    ap.add_argument("--lookback", type=int, default=None)
    ap.add_argument("--notify", type=str, default="console", help="Comma-separated: console,slack,discord")
    ap.add_argument("--out", type=Path, default=Path("output/scan_results.json"))
    ap.add_argument("--config", type=Path, default=None, help="Path to ScannerConfig JSON")
    # IBKR-only flags. Equivalent env vars: IB_HOST, IB_PORT, IB_CLIENT_ID.
    ap.add_argument("--ib-host", type=str, default=None)
    ap.add_argument("--ib-port", type=int, default=None)
    ap.add_argument("--ib-client-id", type=int, default=None)
    return ap.parse_args()


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

    # Push IB flags into the env before constructing the provider — IBConfig reads them there.
    if args.provider in ("ibkr", "ib"):
        if args.ib_host:
            import os
            os.environ["IB_HOST"] = args.ib_host
        if args.ib_port:
            import os
            os.environ["IB_PORT"] = str(args.ib_port)
        if args.ib_client_id:
            import os
            os.environ["IB_CLIENT_ID"] = str(args.ib_client_id)

    log.info("Fetching %d tickers via %s (%s / %dd)", len(tickers), args.provider, cfg.interval, cfg.lookback_days)
    provider = get_provider(args.provider)
    data = provider.get_bars_batch(tickers, cfg.interval, cfg.lookback_days)
    log.info("Got bars for %d/%d tickers", len(data), len(tickers))

    results = scan(data, cfg)
    log.info("Scan produced %d results (top %d)", len(results), cfg.top_n_results)

    ran_at = datetime.now(UTC).isoformat()
    payload = {
        "ran_at": ran_at,
        "provider": args.provider,
        "interval": cfg.interval,
        "lookback_days": cfg.lookback_days,
        "n_candidates_scanned": len(data),
        "n_results": len(results),
        "results": [r.to_dict() for r in results],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    log.info("Wrote %s", args.out)

    for notifier in build_notifiers([n for n in args.notify.split(",") if n]):
        try:
            notifier.send(results)
        except Exception:
            log.exception("Notifier %s failed", type(notifier).__name__)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
