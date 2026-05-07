"""Polygon WebSocket → ScannerRunner adapter.

`PolygonRunnerFeed` subscribes to AM (1-minute aggregate) and Q (NBBO
quote) channels for every symbol the runner knows about, and forwards
each event into the runner / `SymbolState`. Construct it with a started
`PolygonQuoteStreamer` and a `ScannerRunner`, call `start()` to register
subscriptions, `stop()` to tear them down.

The feed is decoupled from the live `PolygonQuoteStreamer` via a small
`Streamer` protocol so unit tests can drive it from a fake.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .runner import ScannerRunner
from .types import Bar

log = logging.getLogger(__name__)

QuoteCallback = Any  # Callable[[dict[str, Any]], None]


class Streamer(Protocol):
    """Minimum subset of `PolygonQuoteStreamer` we depend on."""

    def subscribe(
        self,
        symbol: str,
        *,
        channels: tuple[str, ...] = ...,
        callback: Any,
    ) -> int: ...

    def unsubscribe(self, sub_id: int) -> None: ...


def am_event_to_bar(ev: dict[str, Any]) -> Bar | None:
    """Convert a Polygon AM event to a `Bar`.

    AM keys (Polygon stocks docs): `s` (start ms), `e` (end ms), `o`,
    `h`, `l`, `c`, `v`. We use `e` as the bar's close timestamp to match
    the engine's "close-stamped" convention.
    """
    try:
        return Bar(
            ts_ms=int(ev["e"]),
            open=float(ev["o"]),
            high=float(ev["h"]),
            low=float(ev["l"]),
            close=float(ev["c"]),
            volume=float(ev["v"]),
        )
    except (KeyError, TypeError, ValueError):
        log.debug("Bad AM event: %r", ev)
        return None


def parse_quote(ev: dict[str, Any]) -> tuple[float, float] | None:
    """Pull `(bid, ask)` from a Polygon Q event. None on bad shape."""
    try:
        bid = float(ev["bp"])
        ask = float(ev["ap"])
    except (KeyError, TypeError, ValueError):
        return None
    return bid, ask


class PolygonRunnerFeed:
    """Wires a `PolygonQuoteStreamer` to a `ScannerRunner`.

    Subscriptions are created on `start()` for every symbol currently
    registered with the runner. Symbols added after start need a manual
    `subscribe(symbol)` call (we don't auto-detect runner registry
    changes — the runner doesn't emit add/remove events).
    """

    def __init__(
        self,
        streamer: Streamer,
        runner: ScannerRunner,
        *,
        channels: tuple[str, ...] = ("AM", "Q"),
    ) -> None:
        self._streamer = streamer
        self._runner = runner
        self._channels = channels
        self._sub_ids: dict[str, int] = {}

    def subscribe(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol in self._sub_ids:
            return
        sub_id = self._streamer.subscribe(
            symbol,
            channels=self._channels,
            callback=self._on_event,
        )
        self._sub_ids[symbol] = sub_id

    def unsubscribe(self, symbol: str) -> None:
        symbol = symbol.upper()
        sub_id = self._sub_ids.pop(symbol, None)
        if sub_id is not None:
            self._streamer.unsubscribe(sub_id)

    def start(self) -> None:
        for symbol in list(self._runner.symbols()):
            self.subscribe(symbol)

    def stop(self) -> None:
        for symbol in list(self._sub_ids):
            self.unsubscribe(symbol)

    def _on_event(self, ev: dict[str, Any]) -> None:
        kind = str(ev.get("ev") or "")
        symbol = str(ev.get("sym") or "").upper()
        if not symbol:
            return
        if kind == "AM":
            bar = am_event_to_bar(ev)
            if bar is not None:
                self._runner.on_minute_bar(symbol, bar)
        elif kind == "Q":
            quote = parse_quote(ev)
            if quote is None:
                return
            state = self._runner.state(symbol)
            if state is not None:
                bid, ask = quote
                state.apply_quote(bid, ask)
