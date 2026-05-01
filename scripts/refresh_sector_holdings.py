"""Refresh the on-disk SPDR sector ETF holdings cache from State Street.

Run this on demand (or via cron) to keep the constituent universe in
sync with whatever each sector ETF actually holds today. Output goes to
`${BULLISH_CACHE_DIR}/sector_holdings.json` and the scanner reads it
automatically next time `/scan/sector-rotation` is hit. If the fetch
fails entirely, the scanner silently falls back to the hardcoded
`SECTOR_CONSTITUENTS` snapshot in `scanner.sector_rotation`, so this is
safe to run on a schedule even when SSGA is intermittently unhappy.

Usage:
    python scripts/refresh_sector_holdings.py
    BULLISH_CACHE_DIR=/path/to/cache python scripts/refresh_sector_holdings.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Make the package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.data.etf_holdings import (  # noqa: E402
    refresh_all_constituents,
    save_constituents,
)
from scanner.sector_rotation import SECTOR_ETFS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    cache_dir = Path(
        os.environ.get(
            "BULLISH_CACHE_DIR",
            str(Path(__file__).resolve().parent.parent / "data_cache"),
        )
    )
    out_path = cache_dir / "sector_holdings.json"

    etfs = list(SECTOR_ETFS.keys())
    print(f"Refreshing {len(etfs)} SPDR sector ETFs from SSGA…")
    holdings = refresh_all_constituents(etfs)

    if not holdings:
        print("All SSGA fetches failed — leaving any existing cache untouched.")
        return 1

    save_constituents(out_path, holdings)
    total = sum(len(v) for v in holdings.values())
    print(f"\nSaved {total} total holdings across {len(holdings)} sectors")
    print(f"  -> {out_path}")
    for etf in etfs:
        n = len(holdings.get(etf, []))
        marker = "✓" if n else "✗ (failed; falls back to hardcoded)"
        print(f"  {etf:5s} {n:3d} holdings  {marker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
