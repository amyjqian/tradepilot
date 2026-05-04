"""Cross-cutting warning bus for non-fatal events worth surfacing in
the UI without raising. Currently used for Polygon 429 (rate-limit)
events; structured so IB pacing budgets, broker reconnects, etc. can
publish to the same channel later.

Producers (sync code in providers/brokers) call `warnings.publish(...)`.
The SSE handler in `api.server` subscribes — events are pushed to
every dashboard client so the user sees them in real time.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Warning:
    """One warning event. `kind` is a stable machine-readable tag the
    UI may key styling on; `message` is human-readable; `detail` is an
    optional dict for any extra fields the UI wants to surface."""

    kind: str
    message: str
    ts: float = field(default_factory=time.time)
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _WarningBus:
    """Thread-safe pub/sub for in-memory warnings.

    Publishers may call `publish` from any thread (provider HTTP
    workers, ib_async loop, etc.). Subscribers are SSE handlers
    running on a FastAPI event loop — they pass their loop in so we
    can hand work back via `call_soon_threadsafe`.

    A bounded `deque` keeps the most recent N events so newly-connected
    clients can backfill any warnings they missed."""

    def __init__(self, history: int = 200) -> None:
        self._history: deque[Warning] = deque(maxlen=history)
        self._subs: dict[
            int, tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]
        ] = {}
        self._next_id = 1
        self._lock = threading.Lock()

    def publish(self, w: Warning) -> None:
        payload = w.to_dict()
        with self._lock:
            self._history.append(w)
            subs = list(self._subs.items())
        for _sid, (loop, queue) in subs:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except (RuntimeError, asyncio.QueueFull):
                # Loop closed or subscriber overwhelmed — ignore.
                pass

    def subscribe(
        self, loop: asyncio.AbstractEventLoop
    ) -> tuple[asyncio.Queue[dict[str, Any]], int]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        with self._lock:
            sid = self._next_id
            self._next_id += 1
            self._subs[sid] = (loop, queue)
        return queue, sid

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subs.pop(sub_id, None)

    def recent(self) -> list[dict[str, Any]]:
        """Snapshot of the last N events. Used to backfill on connect."""
        with self._lock:
            return [w.to_dict() for w in self._history]


# Module-level singleton so producers can `from scanner.diagnostics
# import warnings` without ferrying a handle around.
warnings = _WarningBus()
