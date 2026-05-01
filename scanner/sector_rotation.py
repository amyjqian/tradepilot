"""Sector rotation: rank SPDR sector ETFs by relative strength, then scan
the constituents of the strongest sector.

Two-stage flow:
  Stage A — fetch bars for the 11 SPDR sector ETFs + SPY benchmark; rank
            each sector by short-term relative strength vs SPY plus a
            simple trend filter.
  Stage B — pull bars for the constituents of the top-ranked sector and
            hand them to the existing `scanner.engine.scan()`.

Constituent lists are curated approximations of the largest holdings per
sector ETF (top ~25-30 per fund, covering most of the weight). They drift
over time as ETFs rebalance — refresh annually if you care about the
exact composition.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from scanner.config import ScannerConfig
from scanner.engine import ScanResult, scan

log = logging.getLogger(__name__)


SPY_BENCHMARK: str = "SPY"


SECTOR_ETFS: dict[str, str] = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
}


SECTOR_CONSTITUENTS: dict[str, list[str]] = {
    "XLK": [
        "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE",
        "QCOM", "CSCO", "ACN", "IBM", "INTU", "TXN", "AMAT", "LRCX",
        "MU", "ADI", "KLAC", "PANW", "NOW", "CDNS", "SNPS", "FTNT",
        "ANET", "MSI", "ROP", "CTSH", "INTC",
    ],
    "XLF": [
        "BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "AXP", "MS", "BLK",
        "C", "SCHW", "SPGI", "CB", "PGR", "MMC", "PYPL", "USB", "AON",
        "CME", "ICE", "PNC", "AIG", "TFC", "COF", "MET", "AFL", "TRV",
    ],
    "XLE": [
        "XOM", "CVX", "COP", "EOG", "WMB", "SLB", "MPC", "PSX", "OKE",
        "VLO", "PXD", "HES", "OXY", "KMI", "BKR", "FANG", "DVN", "HAL",
        "TRGP", "EQT", "LNG",
    ],
    "XLI": [
        "GE", "CAT", "RTX", "HON", "UNP", "UPS", "BA", "ETN", "DE", "LMT",
        "ADP", "WM", "GD", "TT", "ITW", "EMR", "FDX", "PH", "CSX", "NSC",
        "NOC", "CMI", "PCAR", "MMM", "JCI", "ROK",
    ],
    "XLV": [
        "LLY", "JNJ", "UNH", "MRK", "ABBV", "TMO", "ABT", "PFE", "DHR",
        "AMGN", "BMY", "ELV", "CVS", "MDT", "ISRG", "GILD", "CI", "VRTX",
        "REGN", "BSX", "SYK", "ZTS", "BDX", "HCA", "MRNA",
    ],
    "XLY": [
        "AMZN", "TSLA", "HD", "MCD", "LOW", "BKNG", "TJX", "NKE", "SBUX",
        "ORLY", "CMG", "ABNB", "MAR", "GM", "AZO", "F", "ROST", "HLT",
        "DHI", "LEN", "YUM", "EBAY", "RCL", "DPZ", "LULU",
    ],
    "XLP": [
        "PG", "COST", "WMT", "KO", "PEP", "PM", "MO", "MDLZ", "CL",
        "TGT", "KMB", "MNST", "SYY", "GIS", "STZ", "KDP", "KHC", "EL",
        "ADM", "HSY", "KR", "DG", "TSN", "CHD",
    ],
    "XLU": [
        "NEE", "SO", "DUK", "CEG", "SRE", "AEP", "D", "PCG", "EXC", "XEL",
        "VST", "PEG", "ED", "WEC", "EIX", "AWK", "DTE", "ETR", "PPL",
        "ES", "AEE", "FE", "CMS", "CNP",
    ],
    "XLB": [
        "LIN", "SHW", "ECL", "APD", "FCX", "NEM", "DOW", "DD", "CTVA",
        "PPG", "NUE", "VMC", "MLM", "IFF", "STLD", "BALL", "ALB", "PKG",
        "LYB", "AVY", "CF",
    ],
    "XLRE": [
        "PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "CCI", "DLR",
        "EXR", "VICI", "AVB", "EQR", "SBAC", "WY", "INVH", "VTR", "ARE",
        "MAA", "ESS", "DOC", "UDR", "CPT", "REG",
    ],
    "XLC": [
        "META", "GOOGL", "GOOG", "NFLX", "DIS", "TMUS", "VZ", "T", "CMCSA",
        "EA", "TTWO", "WBD", "OMC", "CHTR", "PARA", "MTCH", "FOX", "FOXA",
        "NWSA", "IPG", "LYV",
    ],
}


def all_universe_tickers() -> list[str]:
    """Deduplicated union of every constituent across all sectors."""
    seen: set[str] = set()
    out: list[str] = []
    for tickers in SECTOR_CONSTITUENTS.values():
        for t in tickers:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _holdings_cache_path() -> Path:
    return Path(
        os.environ.get(
            "BULLISH_CACHE_DIR",
            str(Path(__file__).resolve().parent.parent / "data_cache"),
        )
    ) / "sector_holdings.json"


def get_active_constituents() -> tuple[dict[str, list[str]], str]:
    """Return the constituents map to use right now, preferring the live
    SSGA cache over the hardcoded snapshot.

    The live cache is refreshed manually via
    `python scripts/refresh_sector_holdings.py` (or `./run.sh
    refresh-holdings`). Sectors absent from the live cache fall back
    to `SECTOR_CONSTITUENTS`, so a partial fetch still helps.

    The second return value is a short label suitable for logging:
    `"live (2026-05-01T...)"` or `"hardcoded"`.
    """
    from scanner.data.etf_holdings import load_constituents

    live, fetched_at = load_constituents(_holdings_cache_path())
    if not live:
        return SECTOR_CONSTITUENTS, "hardcoded"
    merged = {**SECTOR_CONSTITUENTS}
    for etf, tickers in live.items():
        if tickers:
            merged[etf] = tickers
    label = f"live ({fetched_at})" if fetched_at else "live"
    return merged, label


class SectorRank(BaseModel):
    etf: str
    name: str
    score: float
    pct_change_1: float
    pct_change_5: float
    excess_return_5_vs_spy: float
    above_ema20: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "etf": self.etf,
            "name": self.name,
            "score": round(self.score, 3),
            "pct_change_1": round(self.pct_change_1, 3),
            "pct_change_5": round(self.pct_change_5, 3),
            "excess_return_5_vs_spy": round(self.excess_return_5_vs_spy, 3),
            "above_ema20": self.above_ema20,
        }


class SectorRotationReport(BaseModel):
    ranked: list[SectorRank] = Field(default_factory=list)
    top_etfs: list[str] = Field(default_factory=list)
    top_names: list[str] = Field(default_factory=list)
    top_constituents: list[str] = Field(default_factory=list)
    # Per-sector constituent map for the top N. Lets the frontend filter
    # the pooled `results` list by sector without re-running the scan.
    top_constituents_by_sector: dict[str, list[str]] = Field(default_factory=dict)
    results: list[ScanResult] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ranked": [r.to_dict() for r in self.ranked],
            "top_etfs": list(self.top_etfs),
            "top_names": list(self.top_names),
            "top_constituents": list(self.top_constituents),
            "top_constituents_by_sector": {
                k: list(v) for k, v in self.top_constituents_by_sector.items()
            },
            "results": [r.to_dict() for r in self.results],
        }


def _pct_return(df: pd.DataFrame, bars: int) -> float:
    if df is None or len(df) <= bars:
        return 0.0
    last = float(df["close"].iloc[-1])
    prev = float(df["close"].iloc[-(bars + 1)])
    if prev <= 0:
        return 0.0
    return (last / prev - 1.0) * 100.0


def _above_ema(df: pd.DataFrame, length: int) -> bool:
    if df is None or len(df) < length:
        return False
    ema = df["close"].ewm(span=length, adjust=False).mean().iloc[-1]
    return bool(df["close"].iloc[-1] > ema)


def rank_sectors(
    sector_data: dict[str, pd.DataFrame],
    spy_df: pd.DataFrame | None,
) -> list[SectorRank]:
    """Score each sector ETF by short-term relative strength + trend.

    Score = (5-bar return − SPY 5-bar return) + trend bonus, where the
    trend bonus is 0.5 if close > EMA20 else 0. Higher is stronger.
    """
    spy_ret_5 = _pct_return(spy_df, 5) if spy_df is not None else 0.0
    ranked: list[SectorRank] = []

    for etf, name in SECTOR_ETFS.items():
        df = sector_data.get(etf)
        if df is None or df.empty:
            continue
        ret_1 = _pct_return(df, 1)
        ret_5 = _pct_return(df, 5)
        excess = ret_5 - spy_ret_5
        trend_up = _above_ema(df, 20)
        score = excess + (0.5 if trend_up else 0.0)
        ranked.append(
            SectorRank(
                etf=etf,
                name=name,
                score=score,
                pct_change_1=ret_1,
                pct_change_5=ret_5,
                excess_return_5_vs_spy=excess,
                above_ema20=trend_up,
            )
        )

    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def run_sector_rotation(
    sector_data: dict[str, pd.DataFrame],
    spy_df: pd.DataFrame | None,
    constituent_data: dict[str, pd.DataFrame],
    cfg: ScannerConfig,
    *,
    top_n: int = 2,
) -> SectorRotationReport:
    """Rank sectors, take the top `top_n`, scan their pooled constituents.

    `top_n` defaults to 2 — broadens the candidate universe enough to
    surface alt leadership when the #1 sector has thin breadth, without
    diluting the rotation signal across half the market. Pass `top_n=1`
    for the strict "strongest sector only" behavior.
    """
    ranked = rank_sectors(sector_data, spy_df)
    if not ranked:
        return SectorRotationReport()

    n = max(1, min(top_n, len(ranked)))
    tops = ranked[:n]

    constituents_map, source = get_active_constituents()
    log.info("Sector rotation using %s constituents", source)

    # Preserve order: sector-rank order, then constituent order within
    # each sector. Dedupe so a stock listed in multiple sectors is only
    # scanned once.
    seen: set[str] = set()
    constituents: list[str] = []
    for top in tops:
        for ticker in constituents_map.get(top.etf, []):
            if ticker not in seen:
                seen.add(ticker)
                constituents.append(ticker)

    snap = {t: df for t, df in constituent_data.items() if t in seen}
    results = scan(snap, cfg) if snap else []

    return SectorRotationReport(
        ranked=ranked,
        top_etfs=[t.etf for t in tops],
        top_names=[t.name for t in tops],
        top_constituents=constituents,
        top_constituents_by_sector={
            t.etf: list(constituents_map.get(t.etf, [])) for t in tops
        },
        results=results,
    )
