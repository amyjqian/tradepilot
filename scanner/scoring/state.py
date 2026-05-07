"""Per-symbol live state for the scoring runner.

`SymbolState` accumulates 1m bars (via `on_minute_bar`), maintains
session-derived running totals (today's cumulative $-volume, today's
high/low, last-bar volume, 20-bar rolling average), and emits a
`SignalContext` snapshot on `build_context(now_ms)`.

Static-per-session fields (`yesterday_high`, `prior_session_close`,
`adv_dollar`, `atr_pct_20d`, daily bars, the TOD volume profile) are
provided once at construction. The runner builds a state object at
session start using its `MarketDataProvider` and feeds bars in as they
arrive. For one-shot REST scans the same state is built by replaying
today's 1m bars through the aggregator.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .aggregator import BarAggregator
from .context import SignalContext
from .types import Bar

ProfileKind = Literal["cum", "30m"]
ProfileLookup = Callable[[int, ProfileKind], float]

# Default time-since-last-bar threshold (seconds) for the halt heuristic.
# Matches `GateThresholds.halt_lookback_seconds`. Pre-session contexts (no
# bars at all) are never flagged as halted.
DEFAULT_HALT_LOOKBACK_SECONDS = 30.0
SPREAD_ROLLING_WINDOW = 60


@dataclass
class SymbolStaticContext:
    """Fields populated once per session from historical data."""

    symbol: str
    session_start_ms: int
    daily_bars: Sequence[Bar]
    yesterday_high: float
    yesterday_low: float
    prior_session_close: float
    adv_dollar: float
    atr_pct_20d: float
    avg_spread_pct: float
    rolling_avg_spread_pct: float
    profile_lookup: ProfileLookup


@dataclass
class SymbolState:
    """Mutable runtime state for one symbol.

    Bar-scope correction (per `PER_15_MINUTE_SCORING_PATCH.md` and
    `CORE_ENGINE_SPEC.md` §4): `bars_1m/5m/15m` are **rolling
    multi-day** bars used for EMAs and RSI computation, not session-only.
    VWAP stays session-anchored via the `session_start_ms` filter inside
    `vwap_session()`. Today-only metadata (`today_high`, `today_low`,
    `today_cum_dollar_volume`, `last_1m_volume`) is updated only by
    today's bars.

    The 1m deque holds enough history for ~5 trading days (5 × 390 RTH
    bars ≈ 1950, with headroom).
    """

    static: SymbolStaticContext
    bars_1m: deque[Bar] = field(default_factory=lambda: deque(maxlen=2500))
    bars_5m: list[Bar] = field(default_factory=list)
    bars_15m: list[Bar] = field(default_factory=list)
    today_cum_dollar_volume: float = 0.0
    today_high: float = -math.inf
    today_low: float = math.inf
    last_1m_volume: float = 0.0
    last_bar_ts_ms: int = 0
    last_bid: float | None = None
    last_ask: float | None = None
    spread_pct_history: deque[float] = field(
        default_factory=lambda: deque(maxlen=SPREAD_ROLLING_WINDOW)
    )
    aggregator: BarAggregator = field(init=False)

    def __post_init__(self) -> None:
        self.aggregator = BarAggregator(self.static.symbol, self.static.session_start_ms)

    def on_minute_bar(self, bar: Bar) -> None:
        if bar.ts_ms <= self.static.session_start_ms:
            return
        self.bars_1m.append(bar)
        self.today_cum_dollar_volume += bar.close * bar.volume
        self.today_high = max(self.today_high, bar.high)
        self.today_low = min(self.today_low, bar.low)
        self.last_1m_volume = bar.volume
        self.last_bar_ts_ms = bar.ts_ms
        ev = self.aggregator.on_minute_bar(bar)
        self.bars_5m.extend(ev.closed_5m)
        self.bars_15m.extend(ev.closed_15m)

    def apply_quote(self, bid: float, ask: float) -> None:
        """Update spread tracking from an NBBO quote.

        `bid` and `ask` must be non-zero positive prices. Crossed or
        zero-width quotes are ignored (Polygon occasionally emits them
        across venue switches and they pollute the rolling average).
        """
        if bid <= 0 or ask <= 0 or ask < bid:
            return
        self.last_bid = bid
        self.last_ask = ask
        mid = (ask + bid) / 2.0
        if mid <= 0:
            return
        spread_pct = (ask - bid) / mid * 100.0
        self.spread_pct_history.append(spread_pct)

    def avg_1m_volume_20bar(self) -> float:
        last20 = list(self.bars_1m)[-20:]
        if not last20:
            return 0.0
        return sum(b.volume for b in last20) / len(last20)

    def current_spread_pct(self) -> float:
        if not self.spread_pct_history:
            return self.static.avg_spread_pct
        return self.spread_pct_history[-1]

    def rolling_avg_spread_pct(self) -> float:
        if not self.spread_pct_history:
            return self.static.rolling_avg_spread_pct
        return sum(self.spread_pct_history) / len(self.spread_pct_history)

    def is_halted(self, now_ms: int, lookback_seconds: float) -> bool:
        # No bars yet (pre-session, just bootstrapped) → not halted.
        if self.last_bar_ts_ms == 0:
            return False
        return (now_ms - self.last_bar_ts_ms) / 1000.0 > lookback_seconds

    def truncate_to(self, eval_ms: int) -> SymbolState:
        """Return a fresh state with rolling history replayed up to `eval_ms`.

        Used by the multi-cadence scan endpoint: each cadence panel
        evaluates at its most recent boundary (e.g. 5m panel at 10:40
        when wall-clock is 10:43), so the 5m-bound signals reflect the
        *closed* 10:35-10:40 bar rather than the still-forming 10:40-10:45
        one. Without this, all cadences would score identically because
        they'd all see the same in-progress bars.

        Rolling-bar aware: prior-session bars are re-aggregated through a
        per-day temp aggregator so 5m/15m windows align to each day's
        09:30 ET start. Today's bars flow through `on_minute_bar` so
        today_* metadata is set.
        """
        fresh = SymbolState(static=self.static)
        truncated_1m = [b for b in self.bars_1m if b.ts_ms <= eval_ms]
        fresh.populate_rolling_history(truncated_1m)
        return fresh

    def populate_rolling_history(self, all_1m_bars: Sequence[Bar]) -> None:
        """Seed `bars_1m`/`5m`/`15m` from a multi-day stream of 1m bars.

        Prior-session bars are aggregated through a temporary per-day
        `BarAggregator` so each day's 5m/15m windows line up to that
        day's 09:30 ET. Today's bars flow through `on_minute_bar`, which
        pushes them through this state's live aggregator (anchored to
        today's session) and updates today_* metadata.

        Idempotent: callers must pass the full intended history; we
        don't dedupe against existing bars on this state.
        """
        from datetime import date as _date
        from datetime import datetime as _datetime
        from zoneinfo import ZoneInfo as _ZoneInfo

        import pandas as pd

        from .aggregator import BarAggregator

        et = _ZoneInfo("America/New_York")
        today_ms = self.static.session_start_ms
        today_date = _datetime.fromtimestamp(today_ms / 1000.0, tz=et).date()

        if not all_1m_bars:
            return

        # Vectorize the date/minute computation. `datetime.fromtimestamp`
        # with a zoneinfo tz is several microseconds per call — for ~10k
        # bars per ticker × 50 tickers that's ~5s of pure tz lookups.
        # Pandas's tz_convert is one C-level operation regardless of N.
        ts_ms_arr = [b.ts_ms for b in all_1m_bars]
        ts_index = pd.to_datetime(ts_ms_arr, unit="ms", utc=True).tz_convert(et)
        minutes = ts_index.hour * 60 + ts_index.minute
        dates = ts_index.date  # numpy array of date objects
        in_rth = (minutes >= 9 * 60 + 30) & (minutes <= 16 * 60)

        # Group bars by ET trading date (RTH bars only).
        by_day: dict[_date, list[Bar]] = {}
        for bar, d, ok in zip(all_1m_bars, dates, in_rth):
            if not ok:
                continue
            by_day.setdefault(d, []).append(bar)

        # Prior days: aggregate through a per-day temp aggregator so
        # window timestamps anchor to *that* day's 09:30 ET.
        for d in sorted(by_day.keys()):
            if d >= today_date:
                continue
            d_session_ms = int(
                _datetime(d.year, d.month, d.day, 9, 30, tzinfo=et).timestamp() * 1000
            )
            temp = BarAggregator(self.static.symbol, d_session_ms)
            for bar in sorted(by_day[d], key=lambda b: b.ts_ms):
                self.bars_1m.append(bar)
                ev = temp.on_minute_bar(bar)
                self.bars_5m.extend(ev.closed_5m)
                self.bars_15m.extend(ev.closed_15m)
            # Flush in-progress 5m/15m at end-of-day so the rolling
            # series doesn't lose the last partial window.
            ip5 = temp.in_progress_5m()
            if ip5 is not None:
                self.bars_5m.append(ip5)
            ip15 = temp.in_progress_15m()
            if ip15 is not None:
                self.bars_15m.append(ip15)

        # Today: through on_minute_bar so today_* metadata + the live
        # aggregator (anchored to today_ms) get updated.
        for bar in sorted(by_day.get(today_date, []), key=lambda b: b.ts_ms):
            self.on_minute_bar(bar)

    def build_context(
        self,
        now_ms: int,
        halt_lookback_seconds: float = DEFAULT_HALT_LOOKBACK_SECONDS,
    ) -> SignalContext:
        # Per spec section 6.1, the bars dict includes the in-progress 5m
        # and 15m as the *last* element. That's what the signals expect when
        # they read `bars["5m"][-1].close` as "now's price."
        bars_5m_full = list(self.bars_5m)
        ip_5m = self.aggregator.in_progress_5m()
        if ip_5m is not None:
            bars_5m_full.append(ip_5m)
        bars_15m_full = list(self.bars_15m)
        ip_15m = self.aggregator.in_progress_15m()
        if ip_15m is not None:
            bars_15m_full.append(ip_15m)

        minute_of_day = max(0, (now_ms - self.static.session_start_ms) // 60_000)
        cum_at_tod = self.static.profile_lookup(minute_of_day, "cum")
        rolling_30m = self.static.profile_lookup(minute_of_day, "30m")

        today_high = self.today_high if self.today_high > -math.inf else 0.0
        today_low = self.today_low if self.today_low < math.inf else 0.0

        return SignalContext(
            symbol=self.static.symbol,
            now_ms=now_ms,
            session_start_ms=self.static.session_start_ms,
            bars={
                "1m": list(self.bars_1m),
                "5m": bars_5m_full,
                "15m": bars_15m_full,
                "daily": list(self.static.daily_bars),
            },
            today_high=today_high,
            today_low=today_low,
            yesterday_high=self.static.yesterday_high,
            yesterday_low=self.static.yesterday_low,
            prior_session_close=self.static.prior_session_close,
            today_cum_dollar_volume=self.today_cum_dollar_volume,
            historical_cum_dollar_volume_at_tod=cum_at_tod,
            historical_30min_dollar_volume=rolling_30m,
            adv_dollar=self.static.adv_dollar,
            atr_pct_20d=self.static.atr_pct_20d,
            avg_spread_pct=self.current_spread_pct(),
            halted_recently=self.is_halted(now_ms, halt_lookback_seconds),
            last_1m_volume=self.last_1m_volume,
            avg_1m_volume_20bar=self.avg_1m_volume_20bar(),
            rolling_avg_spread_pct=self.rolling_avg_spread_pct(),
        )
