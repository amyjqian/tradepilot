"""Polygon real-time WebSocket quote streamer.

Multiplexes a single upstream WebSocket connection across many SSE
clients. Each subscriber hands in a `(symbol, channels, callback)`;
we keep refcounted upstream subscriptions and dispatch incoming events
to all matching callbacks. When the last subscriber for a
`(symbol, channel)` pair drops, we send `unsubscribe` to Polygon.

Threading: a single dedicated daemon thread runs the asyncio loop that
owns the WebSocket. All public methods are thread-safe. Callbacks fire
on the streamer's loop — subscribers should marshal the payload to
their own consumer loop (e.g. with `call_soon_threadsafe`).

Reconnect strategy: on any disconnect we reopen and re-send subscribes
for everything currently in the registry. Backoff is constant
(`reconnect_delay_sec`); Polygon's backend handles bursts gracefully.

Channels (Polygon's standard names):
  - AM: minute aggregate (canonical chart bar)
  - A : second aggregate (high freq)
  - T : trade prints (every print)
  - Q : NBBO quote (bid/ask updates)

Tier notes:
  - Real-time WebSocket access requires Polygon Stocks Developer or
    Advanced. Stocks Starter is REST-only on the real-time side; auth
    will fail there with `auth_failed` and we'll keep retrying — set
    POLYGON_WS_CLUSTER to `wss://delayed.polygon.io/stocks` to use
    the free 15-min-delayed cluster instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable

import websockets

log = logging.getLogger(__name__)


def _make_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that trusts certifi's bundle.

    macOS pip-installed Pythons don't pick up the system keychain by
    default and `ssl.create_default_context()` fails with
    CERTIFICATE_VERIFY_FAILED. httpx already pulls in certifi as a
    transitive dep, so we just point OpenSSL at certifi's CA file —
    same source of truth as the REST provider.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        # If certifi isn't available for some reason, fall back to the
        # platform default — at least we tried.
        return ssl.create_default_context()


QuoteCallback = Callable[[dict[str, Any]], None]


@dataclass
class _Sub:
    id: int
    symbol: str
    channels: tuple[str, ...]
    callback: QuoteCallback


class PolygonQuoteStreamer:
    """Single-process Polygon stocks WebSocket multiplexer."""

    def __init__(
        self,
        api_key: str,
        *,
        cluster: str = "wss://socket.polygon.io/stocks",
        reconnect_delay_sec: float = 3.0,
    ) -> None:
        self._api_key = api_key
        self._cluster = cluster
        self._reconnect_delay = reconnect_delay_sec

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        self._stop_event: threading.Event = threading.Event()

        # Registry: symbol -> channel -> [subs]. Plus the canonical set
        # of `<channel>.<symbol>` strings we've sent upstream so we can
        # restore them on reconnect.
        self._reg_lock = threading.Lock()
        self._next_sub_id = 1
        self._subscribers: dict[str, dict[str, list[_Sub]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._sent_subs: set[str] = set()

        self._ws: Any = None
        # Cleared between connections. Set after the server confirms
        # `auth_success`. We block outgoing sub messages until then.
        self._authed_event: asyncio.Event | None = None
        self._auth_failed_count: int = 0

    @classmethod
    def from_env(cls) -> PolygonQuoteStreamer:
        key = os.environ.get("POLYGON_API_KEY", "").strip()
        if not key:
            raise RuntimeError("POLYGON_API_KEY required for the quote streamer")
        cluster = os.environ.get(
            "POLYGON_WS_CLUSTER", "wss://socket.polygon.io/stocks"
        )
        return cls(api_key=key, cluster=cluster)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._loop_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._auth_failed_count = 0
            ready = threading.Event()
            holder: list[asyncio.AbstractEventLoop] = []

            def _run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                holder.append(loop)
                ready.set()
                try:
                    loop.run_until_complete(self._main())
                except Exception:
                    log.exception("Polygon WS main loop crashed")
                finally:
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception:
                        pass
                    loop.close()

            t = threading.Thread(target=_run, name="polygon-quotes", daemon=True)
            t.start()
            ready.wait()
            self._loop = holder[0]
            self._thread = t

    def stop(self) -> None:
        with self._loop_lock:
            loop = self._loop
            t = self._thread
        if loop is None:
            return
        self._stop_event.set()
        # Closing the WS makes the inner async-for raise; the main loop
        # then sees `_stop_event` and exits cleanly.
        try:
            ws = self._ws
            if ws is not None:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if t is not None:
            t.join(timeout=5)
        with self._loop_lock:
            self._loop = None
            self._thread = None

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------

    def subscribe(
        self,
        symbol: str,
        *,
        channels: tuple[str, ...] = ("AM",),
        callback: QuoteCallback,
    ) -> int:
        """Register a per-event callback. Returns a sub_id used by
        `unsubscribe`. The streamer must be `start()`-ed first.
        """
        symbol = symbol.upper()
        new_pairs: list[str] = []
        with self._reg_lock:
            sub_id = self._next_sub_id
            self._next_sub_id += 1
            sub = _Sub(id=sub_id, symbol=symbol, channels=channels, callback=callback)
            for ch in channels:
                self._subscribers[symbol][ch].append(sub)
                pair = f"{ch}.{symbol}"
                if pair not in self._sent_subs:
                    self._sent_subs.add(pair)
                    new_pairs.append(pair)
        if new_pairs:
            self._send_upstream(
                {"action": "subscribe", "params": ",".join(new_pairs)}
            )
        return sub_id

    def unsubscribe(self, sub_id: int) -> None:
        removed_pairs: list[str] = []
        with self._reg_lock:
            for symbol in list(self._subscribers.keys()):
                by_channel = self._subscribers[symbol]
                for ch in list(by_channel.keys()):
                    by_channel[ch] = [s for s in by_channel[ch] if s.id != sub_id]
                    if not by_channel[ch]:
                        del by_channel[ch]
                        pair = f"{ch}.{symbol}"
                        removed_pairs.append(pair)
                        self._sent_subs.discard(pair)
                if not by_channel:
                    del self._subscribers[symbol]
        if removed_pairs:
            self._send_upstream(
                {"action": "unsubscribe", "params": ",".join(removed_pairs)}
            )

    # ------------------------------------------------------------------
    # Internal: WS plumbing
    # ------------------------------------------------------------------

    def _send_upstream(self, msg: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._async_send(msg), loop)
        except RuntimeError:
            log.debug("Polygon WS send dropped — loop closed")

    async def _async_send(self, msg: dict[str, Any]) -> None:
        ev = self._authed_event
        ws = self._ws
        if ev is None or ws is None:
            # Not yet connected; the reconnect path will re-emit the
            # current subscription set after auth_success.
            return
        try:
            await ev.wait()
            await ws.send(json.dumps(msg))
        except Exception:
            log.debug("Polygon WS send raised", exc_info=True)

    async def _main(self) -> None:
        ssl_ctx = _make_ssl_context() if self._cluster.startswith("wss://") else None
        while not self._stop_event.is_set():
            try:
                self._authed_event = asyncio.Event()
                async with websockets.connect(self._cluster, ssl=ssl_ctx) as ws:
                    self._ws = ws
                    log.info("Polygon WS connected to %s", self._cluster)
                    await self._handle_connection(ws)
            except Exception as exc:
                log.warning("Polygon WS connection error: %s", exc)
            finally:
                self._ws = None
                if self._authed_event is not None:
                    self._authed_event.clear()
            if self._stop_event.is_set():
                break
            # If auth keeps failing the cluster is wrong/forbidden — log
            # loudly so the operator knows to switch tiers/cluster.
            if self._auth_failed_count >= 3:
                log.error(
                    "Polygon WS auth has failed %d times against %s. "
                    "Check POLYGON_API_KEY and the plan's WS entitlement; "
                    "set POLYGON_WS_CLUSTER=wss://delayed.polygon.io/stocks "
                    "for the free delayed feed.",
                    self._auth_failed_count, self._cluster,
                )
            await asyncio.sleep(self._reconnect_delay)

    async def _handle_connection(self, ws: Any) -> None:
        await ws.send(json.dumps({"action": "auth", "params": self._api_key}))

        async for raw in ws:
            try:
                events = json.loads(raw)
            except Exception:
                log.debug("Polygon WS non-JSON frame: %r", raw[:200])
                continue
            if isinstance(events, dict):
                events = [events]
            for ev in events:
                kind = str(ev.get("ev") or "")
                if kind == "status":
                    self._handle_status(ev)
                    continue
                self._dispatch(ev)

    def _handle_status(self, ev: dict[str, Any]) -> None:
        status = str(ev.get("status") or "")
        msg = ev.get("message")
        if status == "auth_success":
            log.info("Polygon WS authenticated")
            self._auth_failed_count = 0
            if self._authed_event is not None:
                self._authed_event.set()
            # Restore subscriptions after a reconnect.
            with self._reg_lock:
                pending = list(self._sent_subs)
            if pending and self._ws is not None:
                try:
                    asyncio.create_task(
                        self._ws.send(
                            json.dumps(
                                {"action": "subscribe", "params": ",".join(pending)}
                            )
                        )
                    )
                except Exception:
                    log.debug("Polygon WS resub send failed", exc_info=True)
        elif status in ("auth_failed", "error"):
            log.warning("Polygon WS status=%s msg=%s", status, msg)
            if status == "auth_failed":
                self._auth_failed_count += 1
        elif status == "success":
            log.debug("Polygon WS sub success: %s", msg)
        else:
            log.debug("Polygon WS status=%s msg=%s", status, msg)

    def _dispatch(self, ev: dict[str, Any]) -> None:
        sym = ev.get("sym") or ev.get("pair") or ""
        kind = str(ev.get("ev") or "")
        if not sym or not kind:
            return
        sym = str(sym).upper()
        with self._reg_lock:
            subs = list(self._subscribers.get(sym, {}).get(kind, []))
        for s in subs:
            try:
                s.callback(ev)
            except Exception:
                log.exception("Polygon WS subscriber callback raised")
