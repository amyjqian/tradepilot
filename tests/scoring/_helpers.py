"""Test helpers for the scoring engine.

`make_bars` and `make_ctx` build minimal `SignalContext` instances. Tests
override only the fields they care about; everything else is set to a
"safe default" that wouldn't trip a gate or disqualifier.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from scanner.scoring.context import SignalContext
from scanner.scoring.types import Bar


def make_bar(
    ts_ms: int = 0,
    *,
    open_: float = 100.0,
    high: float = 100.5,
    low: float = 99.5,
    close: float = 100.0,
    volume: float = 1000.0,
) -> Bar:
    return Bar(ts_ms=ts_ms, open=open_, high=high, low=low, close=close, volume=volume)


def make_bars(
    n: int,
    *,
    start_ms: int = 0,
    step_ms: int = 60_000,
    closes: Sequence[float] | None = None,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    volumes: Sequence[float] | None = None,
) -> list[Bar]:
    out: list[Bar] = []
    for i in range(n):
        c = closes[i] if closes is not None else 100.0
        h = highs[i] if highs is not None else c + 0.5
        lo = lows[i] if lows is not None else c - 0.5
        v = volumes[i] if volumes is not None else 1000.0
        out.append(
            Bar(
                ts_ms=start_ms + i * step_ms,
                open=c,
                high=h,
                low=lo,
                close=c,
                volume=v,
            )
        )
    return out


def make_ctx(
    bars: Mapping[str, Sequence[Bar]] | None = None,
    **overrides: object,
) -> SignalContext:
    defaults: dict[str, object] = dict(
        symbol="TEST",
        now_ms=1,
        session_start_ms=0,
        bars=bars or {"1m": [], "5m": [], "15m": [], "daily": []},
        today_high=110.0,
        today_low=99.0,
        yesterday_high=109.0,
        yesterday_low=98.0,
        prior_session_close=100.0,
        today_cum_dollar_volume=128_000_000.0,
        historical_cum_dollar_volume_at_tod=52_000_000.0,
        historical_30min_dollar_volume=7_800_000.0,
        adv_dollar=480_000_000.0,
        atr_pct_20d=2.10,
        avg_spread_pct=0.04,
        halted_recently=False,
        last_1m_volume=10_000.0,
        avg_1m_volume_20bar=8_000.0,
        rolling_avg_spread_pct=0.04,
    )
    defaults.update(overrides)
    return SignalContext(**defaults)  # type: ignore[arg-type]
