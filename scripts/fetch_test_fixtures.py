"""Fetch and write a deterministic JSON fixture for the scoring engine.

Pulls daily + 1m bars for one symbol on one trading day from a chosen
provider (default: Polygon), truncates the 1m series to a specific ET
evaluation timestamp, builds the same `SymbolState` the runner would,
runs `score_symbol` once, and writes everything (inputs + captured
outputs) to a JSON file under `tests/scoring/fixtures/`.

The committed JSON is the regression artifact. A separate test
(`test_fixture_replay.py`) reads it back, runs `score_symbol` against
the same inputs, and asserts the outputs match the captured numbers
within float tolerance — this is what catches drift in scoring math.

Usage:

  python scripts/fetch_test_fixtures.py \\
      --symbol NVDA --date 2026-04-15 --eval-time 10:42 \\
      [--provider polygon] [--output FILE] [--refresh]

`--refresh` is required to overwrite an existing fixture; otherwise the
script aborts so a stale checkout doesn't silently rewrite committed
test data.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

# Make the project importable when run as a script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scanner.data import get_provider  # noqa: E402
from scanner.scoring.builders import build_state  # noqa: E402
from scanner.scoring.engine import score_symbol_sync  # noqa: E402
from scanner.scoring.types import Bar  # noqa: E402

ET = ZoneInfo("America/New_York")
DEFAULT_OUTPUT_DIR = ROOT / "tests" / "scoring" / "fixtures"


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_eval_time(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()


def _bar_to_dict(b: Bar) -> dict[str, float | int]:
    return {
        "ts_ms": b.ts_ms,
        "open": b.open,
        "high": b.high,
        "low": b.low,
        "close": b.close,
        "volume": b.volume,
    }


def _serialize_context(ctx) -> dict:  # noqa: ANN001 — local helper
    return {
        "symbol": ctx.symbol,
        "now_ms": ctx.now_ms,
        "session_start_ms": ctx.session_start_ms,
        "bars": {
            tf: [_bar_to_dict(b) for b in bars]
            for tf, bars in ctx.bars.items()
        },
        "today_high": ctx.today_high,
        "today_low": ctx.today_low,
        "yesterday_high": ctx.yesterday_high,
        "yesterday_low": ctx.yesterday_low,
        "prior_session_close": ctx.prior_session_close,
        "today_cum_dollar_volume": ctx.today_cum_dollar_volume,
        "historical_cum_dollar_volume_at_tod": ctx.historical_cum_dollar_volume_at_tod,
        "historical_30min_dollar_volume": ctx.historical_30min_dollar_volume,
        "adv_dollar": ctx.adv_dollar,
        "atr_pct_20d": ctx.atr_pct_20d,
        "avg_spread_pct": ctx.avg_spread_pct,
        "halted_recently": ctx.halted_recently,
        "last_1m_volume": ctx.last_1m_volume,
        "avg_1m_volume_20bar": ctx.avg_1m_volume_20bar,
        "rolling_avg_spread_pct": ctx.rolling_avg_spread_pct,
    }


def _serialize_score_result(r) -> dict:  # noqa: ANN001
    return {
        "symbol": r.symbol,
        "timestamp": r.timestamp,
        "base_score": r.base_score,
        "tod_mult": r.tod_mult,
        "final_score": r.final_score,
        "bias_15m": r.bias_15m,
        "tier": r.tier,
        "flags": list(r.flags),
        "components": {
            name: asdict(c) for name, c in r.components.items()
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch a Polygon test fixture for the scoring engine.",
    )
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--date", required=True, type=_parse_date, help="YYYY-MM-DD")
    ap.add_argument(
        "--eval-time",
        required=True,
        type=_parse_eval_time,
        help="HH:MM ET (e.g. 10:42)",
    )
    ap.add_argument("--provider", default="polygon")
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Overwrite an existing fixture file (otherwise abort).",
    )
    args = ap.parse_args()

    eval_dt = datetime.combine(args.date, args.eval_time, tzinfo=ET)
    eval_ms = int(eval_dt.timestamp() * 1000)

    if args.output is None:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{args.symbol.lower()}_{args.date.isoformat()}_{args.eval_time.strftime('%H%M')}.json"
        out_path = DEFAULT_OUTPUT_DIR / fname
    else:
        out_path = args.output

    if out_path.exists() and not args.refresh:
        print(f"Refusing to overwrite {out_path} without --refresh", file=sys.stderr)
        return 2

    provider = get_provider(args.provider)
    state = build_state(provider, args.symbol.upper(), today=args.date)
    if state is None:
        print(f"No data for {args.symbol} on {args.date}", file=sys.stderr)
        return 3

    # Truncate to the evaluation timestamp: drop in-progress bars whose
    # close timestamp exceeds eval_ms, and rebuild the aggregator state
    # by replaying just the in-window bars.
    keep_1m = [b for b in state.bars_1m if b.ts_ms <= eval_ms]
    # Rebuild the runtime state from scratch with the truncated 1m series.
    from scanner.scoring.state import SymbolState

    fresh = SymbolState(static=state.static)
    for b in keep_1m:
        fresh.on_minute_bar(b)

    ctx = fresh.build_context(eval_ms)
    result = score_symbol_sync(ctx)

    payload = {
        "metadata": {
            "symbol": args.symbol.upper(),
            "date": args.date.isoformat(),
            "eval_time_et": args.eval_time.strftime("%H:%M"),
            "eval_ms": eval_ms,
            "provider": args.provider,
        },
        "context": _serialize_context(ctx),
        "score_result": _serialize_score_result(result) if result is not None else None,
    }

    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
