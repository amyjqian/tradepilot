"""List-based indicator helpers used by signals.

These intentionally take `Sequence[Bar]` (or `Sequence[float]`) instead of
pandas frames — the scoring path runs every 60s on every symbol, and the
pandas tax adds up. Existing pandas-based helpers in `scanner.indicators`
remain in use for the legacy engine and for tooling that prefers frames.

All functions are deterministic. Insufficient-data cases return `None` for
single-value APIs and an all-`None` list for series APIs; signal code is
responsible for translating that into `strength=0`.
"""

from __future__ import annotations

from collections.abc import Sequence

from .types import Bar


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average — same convention as pandas ewm(adjust=False).

    Result is `None` for the first `period - 1` indices, then a float.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[float | None] = []
    alpha = 2.0 / (period + 1.0)
    prev: float | None = None
    seeded = False
    seed_sum = 0.0
    seed_count = 0
    for i, v in enumerate(values):
        if not seeded:
            seed_sum += v
            seed_count += 1
            if seed_count < period:
                out.append(None)
                continue
            prev = seed_sum / period
            seeded = True
            out.append(prev)
            continue
        assert prev is not None
        prev = alpha * v + (1.0 - alpha) * prev
        out.append(prev)
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI clipped to [0, 100], None for the first `period` indices."""
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from_avgs(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from_avgs(avg_gain, avg_loss)
    return out


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0 and avg_gain == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return max(0.0, min(100.0, 100.0 - (100.0 / (1.0 + rs))))


def atr_value(bars: Sequence[Bar], period: int = 14) -> list[float | None]:
    """Wilder's ATR over `bars` — absolute price units."""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(bars)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    trs: list[float] = []
    for i in range(1, n):
        prev_close = bars[i - 1].close
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - prev_close),
            abs(bars[i].low - prev_close),
        )
        trs.append(tr)
    if len(trs) < period:
        return out
    atr_prev = sum(trs[:period]) / period
    out[period] = atr_prev
    for i in range(period, len(trs)):
        atr_prev = (atr_prev * (period - 1) + trs[i]) / period
        out[i + 1] = atr_prev
    return out


def atr_pct(bars: Sequence[Bar], period: int = 14) -> list[float | None]:
    """ATR expressed as a percent of close (`atr / close * 100`)."""
    raw = atr_value(bars, period)
    return [
        (v / bars[i].close * 100.0) if v is not None and bars[i].close else None
        for i, v in enumerate(raw)
    ]


def vwap_session(bars: Sequence[Bar], session_start_ms: int) -> list[float | None]:
    """Session-anchored VWAP.

    Bars before `session_start_ms` contribute nothing; values for those
    indices are `None`. The first in-session bar's VWAP equals its typical
    price.
    """
    out: list[float | None] = []
    cum_pv = 0.0
    cum_v = 0.0
    for b in bars:
        if b.ts_ms < session_start_ms:
            out.append(None)
            continue
        typical = (b.high + b.low + b.close) / 3.0
        cum_pv += typical * b.volume
        cum_v += b.volume
        out.append((cum_pv / cum_v) if cum_v > 0 else None)
    return out


def trend_stack_count(
    bars: Sequence[Bar],
    session_start_ms: int,
) -> tuple[float, int] | None:
    """Compute the 4-of-4 trend-stack count on a single timeframe.

    Returns `(strength, count)` where strength is `count / 4` and count is
    in {0,1,2,3,4}, or `None` if insufficient bars (need ≥ 50 closes for a
    full EMA50 + non-`None` VWAP).

    The four checks: close > VWAP, close > EMA9, EMA9 > EMA20, EMA20 > EMA50.
    """
    if len(bars) < 50:
        return None
    closes = [b.close for b in bars]
    ema9 = ema(closes, 9)[-1]
    ema20 = ema(closes, 20)[-1]
    ema50 = ema(closes, 50)[-1]
    vwap_v = vwap_session(bars, session_start_ms)[-1]
    if ema9 is None or ema20 is None or ema50 is None or vwap_v is None:
        return None
    close = closes[-1]
    count = sum(
        [
            close > vwap_v,
            close > ema9,
            ema9 > ema20,
            ema20 > ema50,
        ]
    )
    return count / 4.0, count


def clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
