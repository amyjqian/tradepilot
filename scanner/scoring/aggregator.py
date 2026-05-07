"""1m → 5m / 15m bar aggregator.

Per-symbol state. Feed it 1m bars in monotonic-ascending order; it emits
newly-closed 5m and 15m bars. Aggregation windows are anchored to the
session start (09:30 ET in our universe), so the first 5m closes at 09:35
and the first 15m at 09:45.

A 1m bar's `ts_ms` is treated as its **close** timestamp (end-exclusive
boundary): a bar with `ts_ms = session_start + 60_000` represents trading
from `session_start` through that close, and belongs to the first 5m
window `[session_start, session_start + 5*60_000]`. That same window
closes when the bar with `ts_ms = session_start + 5*60_000` is absorbed.

Gap tolerance: if the feed skips minutes (illiquid stock, halt), the
in-progress window for the gap is closed (with whatever it had) the next
time a bar arrives in a later window, and a fresh window is started for
the new bar. We never fabricate bars across gaps.

Session boundary: when a bar arrives whose timestamp is past the next
session boundary, the in-progress 5m / 15m are flushed and reset. Pass
the session-start of the new day in `next_session_start_ms` to the
constructor (or call `start_session(...)` later).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import Bar


@dataclass
class _InProgressBar:
    window_end_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def absorb(self, bar: Bar) -> None:
        self.high = max(self.high, bar.high)
        self.low = min(self.low, bar.low)
        self.close = bar.close
        self.volume += bar.volume

    def finalize(self) -> Bar:
        return Bar(
            ts_ms=self.window_end_ms,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )

    @classmethod
    def start_from(cls, bar: Bar, window_end_ms: int) -> _InProgressBar:
        return cls(
            window_end_ms=window_end_ms,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )


@dataclass
class AggregatorEvent:
    """Output of a single `on_minute_bar` call."""

    closed_5m: list[Bar] = field(default_factory=list)
    closed_15m: list[Bar] = field(default_factory=list)


class BarAggregator:
    """Per-symbol 1m → 5m/15m roller.

    Construct with the symbol and the current session's 09:30 ET epoch ms.
    `on_minute_bar(bar)` mutates internal state and returns the events
    produced by absorbing that bar.
    """

    def __init__(self, symbol: str, session_start_ms: int) -> None:
        self.symbol = symbol
        self.session_start_ms = session_start_ms
        self._cur_5m: _InProgressBar | None = None
        self._cur_15m: _InProgressBar | None = None

    def start_session(self, session_start_ms: int) -> AggregatorEvent:
        """Reset for a new session; flushes any in-progress bars."""
        events = AggregatorEvent()
        if self._cur_5m is not None:
            events.closed_5m.append(self._cur_5m.finalize())
        if self._cur_15m is not None:
            events.closed_15m.append(self._cur_15m.finalize())
        self.session_start_ms = session_start_ms
        self._cur_5m = None
        self._cur_15m = None
        return events

    def on_minute_bar(self, bar: Bar) -> AggregatorEvent:
        events = AggregatorEvent()
        if bar.ts_ms <= self.session_start_ms:
            # Pre-session bar — ignore. Aggregator only operates within RTH.
            return events
        events.closed_5m.extend(self._roll(bar, period_min=5, current_attr="_cur_5m"))
        events.closed_15m.extend(self._roll(bar, period_min=15, current_attr="_cur_15m"))
        return events

    def _roll(self, bar: Bar, *, period_min: int, current_attr: str) -> list[Bar]:
        win_end_ms = self._window_end_ms(bar.ts_ms, period_min)
        cur: _InProgressBar | None = getattr(self, current_attr)
        emitted: list[Bar] = []
        if cur is None or cur.window_end_ms != win_end_ms:
            if cur is not None:
                emitted.append(cur.finalize())
            cur = _InProgressBar.start_from(bar, win_end_ms)
        else:
            cur.absorb(bar)
        # If this 1m bar exactly hits the window close, the window is done now.
        if bar.ts_ms == win_end_ms:
            emitted.append(cur.finalize())
            cur = None
        setattr(self, current_attr, cur)
        return emitted

    def _window_end_ms(self, ts_ms: int, period_min: int) -> int:
        offset_min = (ts_ms - self.session_start_ms) // 60_000
        # bar at minute N (1-indexed from session start) belongs to window
        # ending at the next multiple of period_min ≥ N.
        idx = (offset_min - 1) // period_min
        end_min = (idx + 1) * period_min
        return self.session_start_ms + end_min * 60_000

    def in_progress_5m(self) -> Bar | None:
        return self._cur_5m.finalize() if self._cur_5m is not None else None

    def in_progress_15m(self) -> Bar | None:
        return self._cur_15m.finalize() if self._cur_15m is not None else None
