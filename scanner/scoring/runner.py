"""ScannerRunner — drives the per-minute scoring cycle.

Two execution modes share the same state machine:

* **Live**: external code feeds 1m bars via `on_minute_bar(symbol, bar)`
  as Polygon's WebSocket pushes them; an asyncio loop ticks every
  `refresh_interval_seconds` and emits a `RankingsUpdate` to subscribers.

* **One-shot**: caller pre-populates each `SymbolState` with replayed
  1m bars (via the same `on_minute_bar` API) and then calls
  `score_all(now_ms)` synchronously to get a single ranking.

Subscribers receive events through `subscribe()`, which returns an
`asyncio.Queue` of `RankingsUpdate`. The API layer (SSE endpoint) is the
expected consumer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from .config import (
    DEFAULT_TOD_MULTIPLIERS,
    DisqualifierConfig,
    GateThresholds,
    ScoringWeights,
    TierThresholds,
    TODMultipliers,
)
from .engine import diagnose_symbol_sync, score_symbol_sync
from .state import SymbolState
from .types import Bar, ScoreResult


def most_recent_cadence_boundary(
    now_ms: int, cadence_seconds: int, session_start_ms: int
) -> int:
    """Return the most recent cadence-aligned timestamp at or before `now_ms`.

    Boundaries are anchored to `session_start_ms` (09:30 ET in our
    universe), matching the BarAggregator's window alignment. So a
    cadence of 300s from session start produces 09:35, 09:40, 09:45, …
    just like the 5m bars themselves close on those marks.

    If `now_ms` is before the session, returns `session_start_ms`.
    """
    if cadence_seconds <= 0:
        return now_ms
    if now_ms <= session_start_ms:
        return session_start_ms
    cadence_ms = cadence_seconds * 1000
    boundaries_passed = (now_ms - session_start_ms) // cadence_ms
    return session_start_ms + boundaries_passed * cadence_ms


@dataclass(frozen=True)
class ScoreRejection:
    """A symbol that didn't make the rankings.

    `reasons` entries are prefixed `gate:` (universe filter) or `dq:`
    (live disqualifier).
    """

    symbol: str
    reasons: list[str]


@dataclass(frozen=True)
class RankingsUpdate:
    """One cycle's output — top-N ranked, plus the cycle timestamp."""

    scanner_id: str
    timestamp_ms: int
    rankings: list[ScoreResult]
    rejected: list[ScoreRejection] = field(default_factory=list)


class ScannerRunner:
    """Per-scanner state holder + cycle driver.

    Thread-safety: `on_minute_bar` is callable from any thread (Polygon's
    WS thread). `score_all` and the asyncio loop must run on the same
    event loop. Internal access uses an `asyncio.Lock` plus a
    threadsafe `call_soon_threadsafe` for cross-thread bar ingestion.
    """

    def __init__(
        self,
        scanner_id: str = "default",
        *,
        refresh_interval_seconds: float = 60.0,
        weights: ScoringWeights | None = None,
        gates: GateThresholds | None = None,
        tod: TODMultipliers | None = None,
        tier_thresholds: TierThresholds | None = None,
        dq_config: DisqualifierConfig | None = None,
        top_n: int = 50,
        align_to_boundary: bool = False,
        boundary_lag_seconds: float = 5.0,
    ) -> None:
        """`align_to_boundary` schedules cycles at clock-aligned cadence
        boundaries (epoch seconds divisible by `refresh_interval_seconds`)
        instead of fixed offsets from the loop's start time. This matches
        the BarAggregator's window alignment, so a cycle fires right
        after a bar closes and the freshly-closed bar is reflected in
        scoring inputs.

        `boundary_lag_seconds` is added to the boundary fire time to
        absorb Polygon's AM-event arrival lag (typically 1–3 s, occasionally
        up to 5 s). Without it, a cycle firing at 13:55:00.0 would race
        the 13:54-13:55 AM event and score against the prior bar.

        Tests using sub-second cadences keep `align_to_boundary=False`
        (the default) so the legacy fixed-interval timer applies.
        """
        self.scanner_id = scanner_id
        self.refresh_interval_seconds = refresh_interval_seconds
        self.weights = weights or ScoringWeights()
        self.gates = gates or GateThresholds()
        self.tod = tod or DEFAULT_TOD_MULTIPLIERS
        self.tier_thresholds = tier_thresholds or TierThresholds()
        self.dq_config = dq_config or DisqualifierConfig()
        self.top_n = top_n
        self.align_to_boundary = align_to_boundary
        self.boundary_lag_seconds = max(0.0, boundary_lag_seconds)

        self._states: dict[str, SymbolState] = {}
        self._subscribers: list[asyncio.Queue[RankingsUpdate]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # -- registration --

    def add_symbol(self, state: SymbolState) -> None:
        self._states[state.static.symbol] = state

    def remove_symbol(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    def symbols(self) -> Iterable[str]:
        return self._states.keys()

    def state(self, symbol: str) -> SymbolState | None:
        return self._states.get(symbol)

    # -- bar ingestion --

    def on_minute_bar(self, symbol: str, bar: Bar) -> None:
        """Thread-safe — Polygon's WS thread can call this directly."""
        state = self._states.get(symbol)
        if state is None:
            return
        loop = self._loop
        if loop is None:
            state.on_minute_bar(bar)
            return
        loop.call_soon_threadsafe(state.on_minute_bar, bar)

    # -- pubsub --

    def subscribe(self) -> asyncio.Queue[RankingsUpdate]:
        queue: asyncio.Queue[RankingsUpdate] = asyncio.Queue(maxsize=64)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[RankingsUpdate]) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    # -- one-shot scoring --

    def score_all(self, now_ms: int) -> list[ScoreResult]:
        rankings, _ = self.score_all_diagnostic(now_ms)
        return rankings

    def score_all_diagnostic(
        self, now_ms: int
    ) -> tuple[list[ScoreResult], list[ScoreRejection]]:
        """Same as `score_all` but also returns the list of rejected
        symbols with their gate/DQ reasons. Use this when surfacing
        diagnostics to the dashboard or logs."""
        halt_secs = self.gates.halt_lookback_seconds
        results: list[ScoreResult] = []
        rejects: list[ScoreRejection] = []
        for symbol, state in self._states.items():
            ctx = state.build_context(now_ms, halt_lookback_seconds=halt_secs)
            r, reasons = diagnose_symbol_sync(
                ctx,
                weights=self.weights,
                gates=self.gates,
                tod=self.tod,
                tier_thresholds=self.tier_thresholds,
                dq_config=self.dq_config,
            )
            if r is not None:
                results.append(r)
            else:
                rejects.append(ScoreRejection(symbol=symbol, reasons=reasons))
        results.sort(key=lambda r: r.final_score, reverse=True)
        return results[: self.top_n], rejects

    # -- async loop --

    async def run(self) -> None:
        """Tick every `refresh_interval_seconds` until `stop()` is called.

        With `align_to_boundary=True`, fire times are clock-aligned to
        the cadence (next epoch second divisible by the cadence) plus
        `boundary_lag_seconds`. This keeps cycles in lockstep with bar
        closes — a 5m runner fires at HH:00:05, HH:05:05, HH:10:05, etc.
        — so each cycle scores against the bar that just closed.
        """
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        try:
            # Initial cycle fires immediately so SSE subscribers don't wait
            # a full cadence for first paint.
            self._cycle()
            while not self._stop.is_set():
                wait = self._next_wait_seconds()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    self._cycle()
        finally:
            self._loop = None

    def _next_wait_seconds(self) -> float:
        """Seconds until the next cycle should fire.

        With `align_to_boundary=True`, each cadence boundary B has a
        scheduled fire time `B + boundary_lag_seconds`. We find the next
        such fire time strictly after `now` and return the delta.
        """
        cadence = self.refresh_interval_seconds
        if not self.align_to_boundary or cadence <= 0:
            return cadence
        now = time.time()
        # Last boundary at or before now.
        last_boundary = (now // cadence) * cadence
        fire = last_boundary + self.boundary_lag_seconds
        if fire <= now:
            # We're past this boundary's lag window — schedule the next.
            fire = last_boundary + cadence + self.boundary_lag_seconds
        return fire - now

    def _cycle(self) -> None:
        now_ms = int(time.time() * 1000)
        rankings, rejected = self.score_all_diagnostic(now_ms)
        update = RankingsUpdate(
            scanner_id=self.scanner_id,
            timestamp_ms=now_ms,
            rankings=rankings,
            rejected=rejected,
        )
        # Non-blocking fanout — drop on full to avoid backpressure stalling
        # the cycle. SSE subscribers that fall behind will see a gap.
        for q in list(self._subscribers):
            try:
                q.put_nowait(update)
            except asyncio.QueueFull:
                pass

    def start(self) -> asyncio.Task[None]:
        """Schedule `run()` on the current event loop."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
