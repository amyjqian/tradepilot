"""PolygonRunnerFeed: AM/Q events → runner state."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from scanner.scoring.feeds import (
    PolygonRunnerFeed,
    am_event_to_bar,
    parse_quote,
)
from scanner.scoring.runner import ScannerRunner
from scanner.scoring.state import SymbolState, SymbolStaticContext

ET = ZoneInfo("America/New_York")
SESSION = int(datetime(2026, 4, 15, 9, 30, tzinfo=ET).timestamp() * 1000)


class _FakeStreamer:
    def __init__(self) -> None:
        self.subs: dict[int, dict[str, Any]] = {}
        self._next = 1

    def subscribe(
        self,
        symbol: str,
        *,
        channels: tuple[str, ...] = ("AM",),
        callback: Any,
    ) -> int:
        sub_id = self._next
        self._next += 1
        self.subs[sub_id] = dict(symbol=symbol, channels=channels, callback=callback)
        return sub_id

    def unsubscribe(self, sub_id: int) -> None:
        self.subs.pop(sub_id, None)

    def emit(self, ev: dict[str, Any]) -> None:
        for sub in list(self.subs.values()):
            if sub["symbol"] == ev.get("sym") and ev.get("ev") in sub["channels"]:
                sub["callback"](ev)


def _profile(_m: int, _k: str) -> float:
    return 1.0


def _runner_with(symbol: str) -> ScannerRunner:
    static = SymbolStaticContext(
        symbol=symbol,
        session_start_ms=SESSION,
        daily_bars=[],
        yesterday_high=110,
        yesterday_low=99,
        prior_session_close=100,
        adv_dollar=480_000_000.0,
        atr_pct_20d=2.10,
        avg_spread_pct=0.04,
        rolling_avg_spread_pct=0.04,
        profile_lookup=_profile,
    )
    runner = ScannerRunner()
    runner.add_symbol(SymbolState(static=static))
    return runner


def test_am_event_to_bar() -> None:
    bar = am_event_to_bar(
        {
            "ev": "AM",
            "sym": "NVDA",
            "s": SESSION,
            "e": SESSION + 60_000,
            "o": 100.0,
            "h": 100.5,
            "l": 99.5,
            "c": 100.2,
            "v": 12345,
        }
    )
    assert bar is not None
    assert bar.ts_ms == SESSION + 60_000
    assert bar.close == 100.2
    assert bar.volume == 12345


def test_am_event_to_bar_handles_bad_shape() -> None:
    assert am_event_to_bar({"ev": "AM"}) is None
    assert am_event_to_bar({"e": "not-a-number", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}) is None


def test_parse_quote() -> None:
    assert parse_quote({"ev": "Q", "bp": 100.0, "ap": 100.10}) == (100.0, 100.10)
    assert parse_quote({"ev": "Q"}) is None


def test_feed_subscribes_existing_runner_symbols_on_start() -> None:
    runner = _runner_with("NVDA")
    streamer = _FakeStreamer()
    feed = PolygonRunnerFeed(streamer, runner)
    feed.start()
    assert len(streamer.subs) == 1
    sub = next(iter(streamer.subs.values()))
    assert sub["symbol"] == "NVDA"
    assert sub["channels"] == ("AM", "Q")


def test_feed_forwards_am_to_runner_state() -> None:
    runner = _runner_with("NVDA")
    streamer = _FakeStreamer()
    feed = PolygonRunnerFeed(streamer, runner)
    feed.start()
    streamer.emit(
        {
            "ev": "AM",
            "sym": "NVDA",
            "s": SESSION,
            "e": SESSION + 60_000,
            "o": 100.0,
            "h": 100.5,
            "l": 99.5,
            "c": 100.2,
            "v": 1000,
        }
    )
    state = runner.state("NVDA")
    assert state is not None
    assert len(state.bars_1m) == 1
    assert state.bars_1m[-1].close == 100.2


def test_feed_forwards_quote_to_state() -> None:
    runner = _runner_with("NVDA")
    streamer = _FakeStreamer()
    feed = PolygonRunnerFeed(streamer, runner)
    feed.start()
    streamer.emit({"ev": "Q", "sym": "NVDA", "bp": 100.0, "ap": 100.05})
    state = runner.state("NVDA")
    assert state is not None
    assert state.last_bid == 100.0 and state.last_ask == 100.05


def test_feed_unsubscribe_removes_subscription() -> None:
    runner = _runner_with("NVDA")
    streamer = _FakeStreamer()
    feed = PolygonRunnerFeed(streamer, runner)
    feed.start()
    feed.unsubscribe("NVDA")
    assert streamer.subs == {}


def test_feed_drops_events_for_unknown_symbol() -> None:
    runner = _runner_with("NVDA")
    streamer = _FakeStreamer()
    feed = PolygonRunnerFeed(streamer, runner)
    feed.start()
    # Pretend the streamer missed an unsubscribe and emitted an event for
    # a symbol the runner no longer tracks.
    runner.remove_symbol("NVDA")
    streamer.emit({"ev": "Q", "sym": "NVDA", "bp": 100.0, "ap": 100.05})  # no exception
