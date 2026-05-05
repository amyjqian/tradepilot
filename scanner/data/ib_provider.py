"""Interactive Brokers TWS/Gateway data provider via ib_async.

Requires TWS or IB Gateway to be running and accepting API connections on the
configured host/port. Port defaults to 8401 (user-configured); IB's stock
defaults are 7496 (live) / 7497 (paper) / 4001 (Gateway live) / 4002 (paper).

Pacing — this is the important part. IB rejects API clients that violate any of:
  - More than 60 historical-data requests in any 10-minute sliding window.
  - More than 6 simultaneous historical-data requests.
  - More than 50 messages per second on the socket.
  - Duplicate historical request within 15 seconds.
  - More than ~2 historical requests per second for the same contract.

We enforce a conservative ceiling (55 requests / 600 s sliding window) via
`_SlidingWindowLimiter`, serialize all requests through a single connection,
and insert a 250 ms spacing between consecutive requests. A full 49-ticker
scan fits comfortably under the limit; a larger universe will transparently
pause for up to ~10 minutes rather than get the client banned.

This provider is read-only: `readonly=True` is passed on connect, so even if
the same client id is reused it cannot place, modify, or cancel orders.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd

from scanner.data.base import MarketDataProvider, validate_bars

if TYPE_CHECKING:
    from ib_async import IB  # pragma: no cover
else:
    IB = Any

log = logging.getLogger(__name__)


_INTERVAL_TO_BAR_SIZE: dict[str, str] = {
    "1m": "1 min",
    "2m": "2 mins",
    "5m": "5 mins",
    "15m": "15 mins",
    "30m": "30 mins",
    "1h": "1 hour",
    "1d": "1 day",
}

# Conservative per-bar-size duration caps (IB rejects oversized requests).
# 2m and 30m caps assume IB uses the same envelope as the neighboring sizes;
# safe to tighten if real-world requests get rejected.
_MAX_DURATION_DAYS: dict[str, int] = {
    "1m": 1,
    "2m": 1,
    "5m": 30,
    "15m": 60,
    "30m": 60,
    "1h": 365,
    "1d": 3650,
}


@dataclass
class IBConfig:
    """Connection + pacing configuration for the IB provider."""

    host: str = "127.0.0.1"
    port: int = 8401
    client_id: int = 27
    connect_timeout_sec: float = 10.0

    # Pacing: IB allows up to 60 historical requests per 10-minute sliding
    # window. We leave headroom so we never trip the ban.
    max_requests_per_window: int = 55
    window_sec: float = 600.0

    # Additional spacing between consecutive requests — guards the 50 msg/sec
    # socket limit and the ~2 req/sec-per-contract limit.
    min_request_spacing_sec: float = 0.25

    # Regular trading hours only. Set False to include pre/post-market bars.
    use_rth: bool = True

    # Parallelism: IB allows up to 6 concurrent historical-data requests.
    # We cap at 5 for headroom.
    max_concurrent_requests: int = 5

    # If the next scan would need to sleep more than this on the pacing
    # limiter, fail fast with PacingBudgetExhausted so the server returns
    # 429 instead of hanging the worker thread for minutes.
    pacing_max_wait_sec: float = 30.0

    @classmethod
    def from_env(cls) -> IBConfig:
        return cls(
            host=os.environ.get("IB_HOST", "127.0.0.1"),
            port=int(os.environ.get("IB_PORT", "8401")),
            client_id=int(os.environ.get("IB_CLIENT_ID", "27")),
        )


class PacingBudgetExhausted(RuntimeError):
    """Raised when the caller asks for more slots than the limiter can
    hand out within its allowed wait. Carries the computed wait so the
    server can surface a clear "try again in N seconds" to the dashboard.
    """

    def __init__(self, message: str, retry_after_sec: float) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class _SlidingWindowLimiter:
    """Thread-safe sliding-window rate limiter.

    `acquire()` blocks until a slot is available within the window.
    `reserve(n, max_wait)` atomically takes `n` slots or raises
    `PacingBudgetExhausted` if it would need to wait longer than
    `max_wait` — so callers can fail-fast instead of hanging the
    FastAPI worker for up to a full window.
    """

    def __init__(self, max_requests: int, window_sec: float) -> None:
        if max_requests <= 0:
            raise ValueError("max_requests must be positive")
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        self._max = max_requests
        self._window = window_sec
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, max_wait_sec: float | None = None) -> None:
        """Take a single slot. If `max_wait_sec` is set and the limiter
        would sleep longer than that, raise `PacingBudgetExhausted`.
        """
        while True:
            with self._lock:
                now = time.time()
                self._evict_expired_locked(now)
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return
                oldest = self._timestamps[0]
                wait = self._window - (now - oldest) + 0.05
            if max_wait_sec is not None and wait > max_wait_sec:
                raise PacingBudgetExhausted(
                    f"IB pacing: at cap ({self._max}/{self._max} in "
                    f"{self._window:.0f}s window); would wait ~{wait:.0f}s "
                    f"(cap {max_wait_sec:.0f}s).",
                    retry_after_sec=wait,
                )
            log.info(
                "IB pacing: at cap (%d/%d in %.0fs window); sleeping %.1fs",
                self._max, self._max, self._window, wait,
            )
            time.sleep(max(wait, 0.1))

    def reserve(self, n: int, max_wait_sec: float | None = None) -> None:
        """Atomically reserve `n` slots. Raises if the request is bigger
        than the whole window cap, or if it would need to wait longer
        than `max_wait_sec`.
        """
        if n <= 0:
            return
        if n > self._max:
            raise PacingBudgetExhausted(
                f"IB pacing: request for {n} slots exceeds cap "
                f"{self._max}/{self._window:.0f}s — infeasible in one batch.",
                retry_after_sec=self._window,
            )
        while True:
            with self._lock:
                now = time.time()
                self._evict_expired_locked(now)
                avail = self._max - len(self._timestamps)
                if avail >= n:
                    for _ in range(n):
                        self._timestamps.append(now)
                    return
                # Wait until enough of the oldest slots have aged out.
                need = n - avail
                target = self._timestamps[need - 1]
                wait = target + self._window - now + 0.05
            if max_wait_sec is not None and wait > max_wait_sec:
                raise PacingBudgetExhausted(
                    f"IB pacing: need {n} slots, {avail} free; would wait "
                    f"~{wait:.0f}s (cap {max_wait_sec:.0f}s). "
                    f"{self._max - avail}/{self._max} used in last "
                    f"{self._window:.0f}s — try again shortly.",
                    retry_after_sec=wait,
                )
            log.info(
                "IB pacing: reserving %d; %d free, sleeping %.1fs",
                n, avail, wait,
            )
            time.sleep(max(wait, 0.1))

    def _evict_expired_locked(self, now: float) -> None:
        while self._timestamps and self._timestamps[0] < now - self._window:
            self._timestamps.popleft()

    def snapshot(self) -> dict[str, Any]:
        """Debug helper — current utilization without taking a slot."""
        with self._lock:
            now = time.time()
            self._evict_expired_locked(now)
            return {
                "in_window": len(self._timestamps),
                "capacity": self._max,
                "window_sec": self._window,
                "oldest_age": (now - self._timestamps[0]) if self._timestamps else None,
            }


class IBProvider(MarketDataProvider):
    """Pull historical OHLCV from a live TWS / IB Gateway session.

    All IB work runs on a single dedicated asyncio loop hosted on its own
    background thread. The `ib_async.IB` client is bound to that loop for
    its whole lifetime, so FastAPI threadpool workers can call in from any
    thread without the socket reader task going stale the moment a request
    lands on a different thread.
    """

    def __init__(self, config: IBConfig | None = None) -> None:
        self._cfg = config or IBConfig.from_env()
        self._limiter = _SlidingWindowLimiter(
            self._cfg.max_requests_per_window, self._cfg.window_sec
        )
        self._ib: IB | None = None  # touched only from the loop thread
        self._last_request_at: float = 0.0
        self._request_lock = threading.Lock()  # guards _space_requests timer
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()

    @property
    def config(self) -> IBConfig:
        return self._cfg

    # ------------------------------------------------------------------
    # Dedicated loop thread
    # ------------------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._loop is not None:
                return self._loop
            ready = threading.Event()
            holder: list[asyncio.AbstractEventLoop] = []

            def _run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                holder.append(loop)
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception:
                        pass
                    loop.close()

            t = threading.Thread(target=_run, name="ib-provider-loop", daemon=True)
            t.start()
            ready.wait()
            self._loop = holder[0]
            self._thread = t
            return self._loop

    def _submit(self, coro: Any, timeout: float | None = None) -> Any:
        """Run `coro` on the dedicated loop and block for the result."""
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    # ------------------------------------------------------------------
    # Connect / disconnect (coroutines run on the loop thread)
    # ------------------------------------------------------------------

    async def _async_connect(self) -> Any:
        import ib_async

        if self._ib is not None:
            if self._ib.isConnected():
                return self._ib
            # Stale handle — drop it so we reconnect fresh.
            self._ib = None
        # Silence the account-level chatter ib_async emits on connect
        # (positions, executions, commission reports). We only need bars.
        # ib_async inherits the logger name "ib_insync.wrapper" from the
        # upstream fork, so quiet both to be safe across versions.
        for name in ("ib_async.wrapper", "ib_insync.wrapper"):
            logging.getLogger(name).setLevel(logging.WARNING)
        ib: Any = ib_async.IB()
        log.info(
            "Connecting to TWS at %s:%d (clientId=%d, readonly=True)",
            self._cfg.host, self._cfg.port, self._cfg.client_id,
        )
        try:
            await ib.connectAsync(
                host=self._cfg.host,
                port=self._cfg.port,
                clientId=self._cfg.client_id,
                timeout=self._cfg.connect_timeout_sec,
                readonly=True,
            )
        except Exception:
            log.exception("IB connection failed")
            raise
        self._ib = ib
        return ib

    async def _async_disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
        self._ib = None

    def disconnect(self) -> None:
        """Disconnect from TWS and tear down the loop thread."""
        with self._loop_lock:
            loop = self._loop
            thread = self._thread
        if loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._async_disconnect(), loop)
            fut.result(timeout=5.0)
        except Exception:
            log.exception("IB disconnect failed")
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if thread is not None:
            thread.join(timeout=2.0)
        with self._loop_lock:
            self._loop = None
            self._thread = None

    def pacing_status(self) -> dict[str, Any]:
        """Return current rate-limiter utilization (for logging / dashboards)."""
        return self._limiter.snapshot()

    # ------------------------------------------------------------------
    # MarketDataProvider interface
    # ------------------------------------------------------------------

    async def _async_fetch_one(
        self, ticker: str, bar_size: str, duration_str: str
    ) -> pd.DataFrame:
        import ib_async

        ib = await self._async_connect()
        contract = ib_async.Stock(ticker, "SMART", "USD")
        try:
            bars = await ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration_str,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=self._cfg.use_rth,
                formatDate=2,  # return UTC-aware datetimes
                keepUpToDate=False,
            )
        except Exception as exc:
            log.exception("IB reqHistoricalData failed for %s", ticker)
            raise RuntimeError(f"IB request failed for {ticker}: {exc}") from exc
        if not bars:
            raise ValueError(
                f"IB returned no bars for {ticker} "
                f"(duration={duration_str}, barSize={bar_size})"
            )
        return self._bars_to_frame(bars)

    def get_bars(self, ticker: str, interval: str, lookback_days: int) -> pd.DataFrame:
        bar_size = _INTERVAL_TO_BAR_SIZE.get(interval)
        if bar_size is None:
            raise ValueError(
                f"Unsupported interval for IB: {interval!r}; "
                f"expected one of {sorted(_INTERVAL_TO_BAR_SIZE)}"
            )
        duration_days = max(1, min(lookback_days, _MAX_DURATION_DAYS[interval]))
        duration_str = f"{duration_days} D"

        with self._request_lock:
            self._limiter.acquire(max_wait_sec=self._cfg.pacing_max_wait_sec)
            self._space_requests()
            return self._submit(self._async_fetch_one(ticker, bar_size, duration_str))

    async def _async_fetch_batch(
        self,
        tickers: list[str],
        bar_size: str,
        duration_str: str,
        max_concurrent: int,
    ) -> list[tuple[str, pd.DataFrame | Exception | None]]:
        import ib_async

        ib = await self._async_connect()
        sem = asyncio.Semaphore(max_concurrent)

        async def _fetch_one(
            ticker: str,
        ) -> tuple[str, pd.DataFrame | Exception | None]:
            async with sem:
                contract = ib_async.Stock(ticker, "SMART", "USD")
                try:
                    bars = await ib.reqHistoricalDataAsync(
                        contract,
                        endDateTime="",
                        durationStr=duration_str,
                        barSizeSetting=bar_size,
                        whatToShow="TRADES",
                        useRTH=self._cfg.use_rth,
                        formatDate=2,
                        keepUpToDate=False,
                    )
                except Exception as exc:
                    return ticker, exc
                if not bars:
                    return ticker, None
                try:
                    return ticker, self._bars_to_frame(bars)
                except Exception as exc:
                    return ticker, exc

        return await asyncio.gather(*[_fetch_one(t) for t in tickers])

    def get_bars_batch(
        self, tickers: list[str], interval: str, lookback_days: int
    ) -> dict[str, pd.DataFrame]:
        """Parallel batch fetch.

        Issues up to `max_concurrent_requests` historical-data requests in
        parallel via `ib.reqHistoricalDataAsync`, coordinated by an asyncio
        semaphore. All requests still pass through the sliding-window rate
        limiter, so the 55 / 600 s budget is respected even across the batch.

        For N=20 tickers on 1-minute bars (~1-3 s per IB call), this typically
        cuts wall-clock from ~40 s (serial) to ~8 s (5-way parallel).
        """
        if not tickers:
            return {}

        bar_size = _INTERVAL_TO_BAR_SIZE.get(interval)
        if bar_size is None:
            raise ValueError(
                f"Unsupported interval for IB: {interval!r}; "
                f"expected one of {sorted(_INTERVAL_TO_BAR_SIZE)}"
            )
        duration_days = max(1, min(lookback_days, _MAX_DURATION_DAYS[interval]))
        duration_str = f"{duration_days} D"

        # Reserve all rate-limit slots atomically. Fails fast if the
        # batch would require a long wait — better than hanging /scan
        # for up to a full window while the user has no feedback.
        self._limiter.reserve(
            len(tickers), max_wait_sec=self._cfg.pacing_max_wait_sec
        )

        t0 = time.time()
        log.info(
            "IB batch fetching %d tickers (interval=%s, duration=%s, max_concurrent=%d)",
            len(tickers), interval, duration_str, self._cfg.max_concurrent_requests,
        )
        results = self._submit(
            self._async_fetch_batch(
                tickers, bar_size, duration_str, self._cfg.max_concurrent_requests
            )
        )
        elapsed = time.time() - t0
        out: dict[str, pd.DataFrame] = {}
        for ticker, df_or_exc in results:
            if isinstance(df_or_exc, Exception):
                log.warning("IB get_bars skipped %s: %s", ticker, df_or_exc)
            elif df_or_exc is not None:
                out[ticker] = df_or_exc
        log.info(
            "IB batch done: %d/%d frames in %.1fs (%.2fs/ticker)",
            len(out), len(tickers), elapsed, elapsed / max(len(tickers), 1),
        )
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _space_requests(self) -> None:
        spacing = self._cfg.min_request_spacing_sec
        if spacing <= 0:
            return
        elapsed = time.time() - self._last_request_at
        if 0 < elapsed < spacing:
            time.sleep(spacing - elapsed)
        self._last_request_at = time.time()

    @staticmethod
    def _bars_to_frame(bars: list[Any]) -> pd.DataFrame:
        idx = pd.to_datetime([b.date for b in bars])
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        df = pd.DataFrame(
            {
                "open": [float(b.open) for b in bars],
                "high": [float(b.high) for b in bars],
                "low": [float(b.low) for b in bars],
                "close": [float(b.close) for b in bars],
                "volume": [float(b.volume) for b in bars],
            },
            index=idx,
        ).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return validate_bars(df)
