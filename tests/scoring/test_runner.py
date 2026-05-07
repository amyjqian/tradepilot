"""ScannerRunner: state machine + one-shot scoring + asyncio loop."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scanner.scoring.runner import ScannerRunner
from scanner.scoring.state import SymbolState, SymbolStaticContext
from scanner.scoring.types import Bar

ET = ZoneInfo("America/New_York")
SESSION_START = int(datetime(2026, 4, 15, 9, 30, tzinfo=ET).timestamp() * 1000)


def _profile(_minute_of_day: int, kind: str) -> float:
    return {"cum": 50_000_000.0, "30m": 7_000_000.0}[kind]


def _make_state(symbol: str, *, with_history: bool = True) -> SymbolState:
    daily_bars = [
        Bar(ts_ms=i, open=100.0, high=101.0, low=99.0, close=100.0, volume=1_000_000.0)
        for i in range(30)
    ]
    static = SymbolStaticContext(
        symbol=symbol,
        session_start_ms=SESSION_START,
        daily_bars=daily_bars,
        yesterday_high=124.5,
        yesterday_low=119.4,
        prior_session_close=100.0,
        adv_dollar=480_000_000.0,
        atr_pct_20d=2.10,
        avg_spread_pct=0.04,
        rolling_avg_spread_pct=0.04,
        profile_lookup=_profile,
    )
    state = SymbolState(static=static)
    if with_history:
        # Replay 72 ascending 1m bars 09:30 → 10:42.
        for i in range(1, 73):
            ts = SESSION_START + i * 60_000
            close = 100.0 + i * 0.05
            state.on_minute_bar(
                Bar(
                    ts_ms=ts,
                    open=close - 0.05,
                    high=close + 0.10,
                    low=close - 0.10,
                    close=close,
                    volume=10_000.0,
                )
            )
    return state


def test_score_all_returns_results_sorted_by_final_score() -> None:
    runner = ScannerRunner()
    runner.add_symbol(_make_state("AAA"))
    runner.add_symbol(_make_state("BBB"))
    now = SESSION_START + 72 * 60_000
    results = runner.score_all(now)
    assert len(results) >= 1
    scores = [r.final_score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_state_aggregates_5m_and_15m() -> None:
    state = _make_state("X")
    # 72 minute-bars → 14 closed 5m windows (last 5m 10:40–10:45 is in-progress)
    assert len(state.bars_5m) == 14
    # → 4 closed 15m windows (last 15m 10:30–10:45 in-progress)
    assert len(state.bars_15m) == 4


def test_one_shot_cycle_updates_state_and_scores() -> None:
    runner = ScannerRunner(top_n=10)
    runner.add_symbol(_make_state("X", with_history=False))
    state = runner.state("X")
    assert state is not None
    # Feed bars one at a time and verify state grows.
    for i in range(1, 31):
        ts = SESSION_START + i * 60_000
        runner.on_minute_bar(
            "X",
            Bar(ts_ms=ts, open=100, high=100.5, low=99.5, close=100.0 + i * 0.05, volume=10_000),
        )
    assert len(state.bars_1m) == 30
    # Score; with only 30 bars and no daily warmup mismatch, we may get a result
    # or None — both are valid. The point is no exception.
    runner.score_all(SESSION_START + 30 * 60_000)


def test_subscribe_and_run_cycle_emits_events() -> None:
    async def main() -> None:
        runner = ScannerRunner(refresh_interval_seconds=0.05)
        runner.add_symbol(_make_state("X"))
        queue = runner.subscribe()
        task = runner.start()
        try:
            update = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert update.scanner_id == "default"
            assert isinstance(update.rankings, list)
        finally:
            await runner.stop()
        assert task.done()

    asyncio.run(main())


def test_on_minute_bar_for_unknown_symbol_is_noop() -> None:
    runner = ScannerRunner()
    runner.on_minute_bar(
        "UNKNOWN",
        Bar(ts_ms=SESSION_START + 60_000, open=1, high=1, low=1, close=1, volume=1),
    )  # no exception


def test_align_to_boundary_off_returns_fixed_interval() -> None:
    runner = ScannerRunner(refresh_interval_seconds=60.0)
    assert runner._next_wait_seconds() == 60.0


def test_align_to_boundary_fires_lag_after_boundary() -> None:
    """Each cadence boundary B is scheduled to fire at B + lag."""
    from unittest.mock import patch

    runner = ScannerRunner(
        refresh_interval_seconds=60.0,
        align_to_boundary=True,
        boundary_lag_seconds=5.0,
    )
    # Just before a boundary: 0.1s before the 60-second mark.
    with patch("scanner.scoring.runner.time.time", return_value=59.9):
        # Last boundary = 0; fire = 5s; 5 <= 59.9 → push to next boundary
        # (60) + 5s = 65. Wait = 65 - 59.9 = 5.1s.
        assert abs(runner._next_wait_seconds() - 5.1) < 0.001
    # Just past a boundary: 0.1s after.
    with patch("scanner.scoring.runner.time.time", return_value=60.1):
        # Last boundary = 60; fire = 65; wait = 4.9.
        assert abs(runner._next_wait_seconds() - 4.9) < 0.001
    # Past the lag window: 5.5s past a boundary → schedule next boundary's fire.
    with patch("scanner.scoring.runner.time.time", return_value=65.5):
        # Last boundary = 60; fire = 65 (already past); next fire = 125;
        # wait = 59.5.
        assert abs(runner._next_wait_seconds() - 59.5) < 0.001
