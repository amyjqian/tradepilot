"""BarAggregator: 1m → 5m / 15m correctness."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scanner.scoring.aggregator import BarAggregator
from scanner.scoring.types import Bar

ET = ZoneInfo("America/New_York")
SESSION_START_2026_04_15 = int(datetime(2026, 4, 15, 9, 30, tzinfo=ET).timestamp() * 1000)


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=ET).timestamp() * 1000)


def _minute_bar(close_ms: int, *, close: float = 100.0, volume: float = 1000.0) -> Bar:
    return Bar(
        ts_ms=close_ms,
        open=close - 0.05,
        high=close + 0.10,
        low=close - 0.10,
        close=close,
        volume=volume,
    )


def test_75_minute_bars_produce_15_5m_and_5_15m_closes() -> None:
    """09:30→10:45 = 75 minutes → 15 closed 5m + 5 closed 15m."""
    agg = BarAggregator("NVDA", SESSION_START_2026_04_15)
    closed_5m = 0
    closed_15m = 0
    for i in range(1, 76):
        ts = SESSION_START_2026_04_15 + i * 60_000
        ev = agg.on_minute_bar(_minute_bar(ts))
        closed_5m += len(ev.closed_5m)
        closed_15m += len(ev.closed_15m)
    assert closed_5m == 15
    assert closed_15m == 5


def test_5m_close_boundary_is_exact() -> None:
    """The 1m bar at 09:35 closes the first 5m window."""
    agg = BarAggregator("X", SESSION_START_2026_04_15)
    ev1 = agg.on_minute_bar(_minute_bar(SESSION_START_2026_04_15 + 60_000))  # 09:31
    assert ev1.closed_5m == []
    # Bars 09:32–09:34
    for i in range(2, 5):
        ev = agg.on_minute_bar(
            _minute_bar(SESSION_START_2026_04_15 + i * 60_000)
        )
        assert ev.closed_5m == []
    # Bar at 09:35 — closes the [09:30, 09:35] window
    ev_close = agg.on_minute_bar(_minute_bar(SESSION_START_2026_04_15 + 5 * 60_000))
    assert len(ev_close.closed_5m) == 1
    assert ev_close.closed_5m[0].ts_ms == SESSION_START_2026_04_15 + 5 * 60_000


def test_ohlcv_invariants() -> None:
    """5m close = sum of constituent volumes; high = max; low = min."""
    agg = BarAggregator("X", SESSION_START_2026_04_15)
    closes = [100.0, 101.0, 99.5, 100.5, 102.0]
    last_event = None
    for i, c in enumerate(closes, start=1):
        ts = SESSION_START_2026_04_15 + i * 60_000
        bar = Bar(
            ts_ms=ts,
            open=c - 0.5,
            high=c + 0.5,
            low=c - 0.5,
            close=c,
            volume=1_000.0 * i,  # 1k, 2k, 3k, 4k, 5k
        )
        last_event = agg.on_minute_bar(bar)
    assert last_event is not None
    assert len(last_event.closed_5m) == 1
    closed = last_event.closed_5m[0]
    assert closed.high == pytest.approx(102.5)  # max(c + 0.5)
    assert closed.low == pytest.approx(99.0)  # min(c - 0.5)
    assert closed.close == pytest.approx(102.0)  # last bar's close
    assert closed.volume == pytest.approx(15_000.0)  # 1+2+3+4+5 thousand


def test_gap_closes_old_window_starts_new() -> None:
    """If a bar skips ahead, the prior in-progress window is finalized."""
    agg = BarAggregator("X", SESSION_START_2026_04_15)
    agg.on_minute_bar(_minute_bar(SESSION_START_2026_04_15 + 60_000))  # 09:31
    # Skip to 09:42 (a gap from 09:32 to 09:41).
    ev = agg.on_minute_bar(_minute_bar(SESSION_START_2026_04_15 + 12 * 60_000))
    # First 5m window [09:30, 09:35] had only one bar (09:31); should close.
    # Second [09:35, 09:40] had nothing → not emitted (we don't fabricate).
    # Third [09:40, 09:45] now has 09:42's bar in progress.
    assert len(ev.closed_5m) == 1
    assert ev.closed_5m[0].ts_ms == SESSION_START_2026_04_15 + 5 * 60_000


def test_15m_first_close_is_at_945() -> None:
    """15m windows align to 09:30, 09:45, 10:00, ... — first close at 09:45."""
    agg = BarAggregator("X", SESSION_START_2026_04_15)
    closed_15m: list[int] = []
    for i in range(1, 16):
        ev = agg.on_minute_bar(_minute_bar(SESSION_START_2026_04_15 + i * 60_000))
        closed_15m.extend(b.ts_ms for b in ev.closed_15m)
    assert closed_15m == [SESSION_START_2026_04_15 + 15 * 60_000]


def test_dst_spring_forward_2026_03_08() -> None:
    """Session starts at 09:30 ET — same wall-clock minute either side of DST.

    DST springs forward at 02:00 ET on 2026-03-08 (jumps to 03:00). The
    9:30 ET session start is unambiguous; we just need the aggregator to
    use the supplied session_start_ms verbatim and not try to infer
    anything.
    """
    session_start = int(datetime(2026, 3, 9, 9, 30, tzinfo=ET).timestamp() * 1000)
    agg = BarAggregator("X", session_start)
    closed_5m = 0
    for i in range(1, 11):
        ev = agg.on_minute_bar(_minute_bar(session_start + i * 60_000))
        closed_5m += len(ev.closed_5m)
    assert closed_5m == 2  # 5 and 10 minutes of bars → 2 closed 5m windows


def test_pre_session_bars_ignored() -> None:
    """Bars before session_start are dropped, not aggregated into a phantom window."""
    agg = BarAggregator("X", SESSION_START_2026_04_15)
    pre = _minute_bar(SESSION_START_2026_04_15 - 60_000)  # 09:29
    ev = agg.on_minute_bar(pre)
    assert ev.closed_5m == [] and ev.closed_15m == []
    # First in-session bar should not include the pre-session volume.
    ev1 = agg.on_minute_bar(_minute_bar(SESSION_START_2026_04_15 + 60_000, volume=2_000))
    in_progress = agg.in_progress_5m()
    assert in_progress is not None
    assert in_progress.volume == 2_000


def test_start_session_resets_state() -> None:
    """start_session flushes in-progress and rebases the aggregator."""
    agg = BarAggregator("X", SESSION_START_2026_04_15)
    agg.on_minute_bar(_minute_bar(SESSION_START_2026_04_15 + 60_000))
    flushed = agg.start_session(_ms(2026, 4, 16, 9, 30))
    assert len(flushed.closed_5m) == 1  # the in-progress 5m got finalized
    # New session in fresh state
    assert agg.in_progress_5m() is None
    assert agg.in_progress_15m() is None
