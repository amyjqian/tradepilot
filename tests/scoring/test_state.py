"""SymbolState: quote tracking + halt heuristic."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from scanner.scoring.state import SymbolState, SymbolStaticContext
from scanner.scoring.types import Bar

ET = ZoneInfo("America/New_York")
SESSION = int(datetime(2026, 4, 15, 9, 30, tzinfo=ET).timestamp() * 1000)


def _profile(_m: int, _k: str) -> float:
    return 1.0


def _state() -> SymbolState:
    static = SymbolStaticContext(
        symbol="X",
        session_start_ms=SESSION,
        daily_bars=[],
        yesterday_high=110.0,
        yesterday_low=99.0,
        prior_session_close=100.0,
        adv_dollar=480_000_000.0,
        atr_pct_20d=2.10,
        avg_spread_pct=0.04,
        rolling_avg_spread_pct=0.04,
        profile_lookup=_profile,
    )
    return SymbolState(static=static)


def test_apply_quote_updates_spread() -> None:
    s = _state()
    s.apply_quote(bid=100.0, ask=100.10)
    assert s.last_bid == 100.0 and s.last_ask == 100.10
    # spread% = (0.10) / 100.05 * 100 ≈ 0.0999
    assert abs(s.current_spread_pct() - 0.09995) < 1e-3


def test_apply_quote_rejects_crossed_or_zero() -> None:
    s = _state()
    s.apply_quote(bid=0, ask=100.0)
    s.apply_quote(bid=101.0, ask=100.0)  # crossed
    s.apply_quote(bid=-1, ask=100.0)
    assert s.last_bid is None
    # Falls back to static when history is empty
    assert s.current_spread_pct() == 0.04


def test_rolling_spread_avg() -> None:
    s = _state()
    for spread_bps in (0.05, 0.10, 0.15):
        # synthesize a bid/ask producing that spread%
        mid = 100.0
        half = mid * spread_bps / 100.0 / 2.0
        s.apply_quote(bid=mid - half, ask=mid + half)
    assert abs(s.rolling_avg_spread_pct() - 0.10) < 1e-3


def test_halt_heuristic_pre_session_is_not_halted() -> None:
    s = _state()
    # No bars at all (just constructed) — not halted.
    assert not s.is_halted(now_ms=SESSION + 60_000, lookback_seconds=30.0)


def test_halt_heuristic_after_silence() -> None:
    s = _state()
    bar = Bar(ts_ms=SESSION + 60_000, open=100, high=100.5, low=99.5, close=100, volume=1000)
    s.on_minute_bar(bar)
    # 10 seconds later → not halted
    assert not s.is_halted(now_ms=SESSION + 60_000 + 10_000, lookback_seconds=30.0)
    # 60 seconds later → halted
    assert s.is_halted(now_ms=SESSION + 60_000 + 60_000, lookback_seconds=30.0)


def test_populate_rolling_history_keeps_prior_day_bars() -> None:
    """Multi-day 1m bars produce per-day-aggregated 5m/15m history.

    Validates the bar-scope correction: prior sessions contribute to
    rolling bars_5m/15m so EMA21 (15m) can seed before 14:45 ET on
    session day 1.
    """
    state = _state()
    today_ms = SESSION
    yesterday_session_ms = int(
        datetime(2026, 4, 14, 9, 30, tzinfo=ET).timestamp() * 1000
    )

    bars: list[Bar] = []
    # Yesterday: 78 1m bars (full RTH-ish span) starting 09:31
    for i in range(1, 79):
        ts = yesterday_session_ms + i * 60_000
        bars.append(
            Bar(ts_ms=ts, open=100, high=100.5, low=99.5, close=100, volume=1000)
        )
    # Today: 30 1m bars starting 09:31
    for i in range(1, 31):
        ts = today_ms + i * 60_000
        bars.append(
            Bar(ts_ms=ts, open=101, high=101.5, low=100.5, close=101, volume=1500)
        )

    state.populate_rolling_history(bars)
    assert len(state.bars_1m) == 78 + 30  # both sessions retained
    # Yesterday: 78 1m → 15 closed 5m + 1 in-progress = 16; or 15 closed
    # if 78 lands exactly on a 5m boundary. We're tolerant — just need
    # to see that prior-day windows are present.
    assert len(state.bars_5m) >= 15
    # Today's metadata only reflects today's bars (1500 vol each, 30 bars).
    assert state.last_1m_volume == 1500
    assert state.today_cum_dollar_volume == 30 * 101 * 1500


def test_truncate_to_preserves_rolling_history() -> None:
    """truncate_to must replay prior-day bars too, not just today's."""
    state = _state()
    yesterday_session_ms = int(
        datetime(2026, 4, 14, 9, 30, tzinfo=ET).timestamp() * 1000
    )
    bars: list[Bar] = []
    for i in range(1, 79):
        ts = yesterday_session_ms + i * 60_000
        bars.append(Bar(ts_ms=ts, open=100, high=100.5, low=99.5, close=100, volume=1000))
    for i in range(1, 31):
        ts = SESSION + i * 60_000
        bars.append(Bar(ts_ms=ts, open=101, high=101.5, low=100.5, close=101, volume=1500))
    state.populate_rolling_history(bars)

    # Truncate to mid-today
    eval_ms = SESSION + 15 * 60_000  # 09:45 today
    fresh = state.truncate_to(eval_ms)
    # Yesterday's bars should still be in the truncated state's rolling history
    assert any(b.ts_ms < SESSION for b in fresh.bars_1m)
    # Today only has 15 bars after truncation
    today_count = sum(1 for b in fresh.bars_1m if b.ts_ms > SESSION)
    assert today_count == 15


def test_build_context_uses_live_spread_and_halt() -> None:
    s = _state()
    s.on_minute_bar(
        Bar(ts_ms=SESSION + 60_000, open=100, high=100.5, low=99.5, close=100, volume=1000)
    )
    s.apply_quote(bid=100.0, ask=100.20)
    # At session_start + 90s (no halt):
    ctx = s.build_context(SESSION + 90_000, halt_lookback_seconds=30.0)
    assert ctx.halted_recently is False
    assert abs(ctx.avg_spread_pct - 0.1996) < 1e-3
    # At session_start + 5min (last bar at 1min → halted):
    ctx2 = s.build_context(SESSION + 300_000, halt_lookback_seconds=30.0)
    assert ctx2.halted_recently is True
