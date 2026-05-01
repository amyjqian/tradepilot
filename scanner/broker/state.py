"""Live broker state cache + SSE fanout + circuit-breaker bookkeeping.

`BrokerState` is the bridge between ib_async events (which fire on the
broker's loop thread) and the FastAPI request handlers (which run on a
different loop). Each SSE subscriber registers an `asyncio.Queue` plus
the `loop` it lives on; when an event fires, we hand the payload to that
loop via `call_soon_threadsafe(queue.put_nowait, event)`. This keeps
every queue's puts in its owning loop, which is what asyncio.Queue
requires.

State here is intentionally separate from `IBBroker` so the kill-switch
and journal logic is testable without touching ib_async.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from scanner.broker.journal import Journal, today_et

log = logging.getLogger(__name__)


# Hard cap on how many filled-but-uncleaned order metadata entries we
# keep around. Each order placed via the API records {planned_stop,
# score} so a later execDetails event can persist it; we evict the
# oldest once we exceed this.
_MAX_PENDING_META = 1024


@dataclass
class _Subscriber:
    queue: asyncio.Queue[dict[str, Any]]
    loop: asyncio.AbstractEventLoop
    id: int


@dataclass
class _OrderMeta:
    """Captured at order-submit time, applied when the fill commission
    report comes in. Keyed by IB orderId (per-session unique) — note this
    is NOT permId. permId only appears once IB acknowledges the order, so
    the only stable id we have at submit time is orderId.
    """
    planned_stop: float | None = None
    score_at_entry: float | None = None
    submitted_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())


class BrokerState:
    """Singleton-per-broker cache of orders / positions / account values.

    All public mutators are safe to call from any thread; subscribers
    receive deltas on their own loop.
    """

    def __init__(self, journal: Journal, *, max_drawdown_pct: float) -> None:
        self.journal = journal
        # `0` (or negative) disables the circuit breaker entirely.
        self.max_drawdown_pct: float = max(0.0, float(max_drawdown_pct))

        self._lock = threading.Lock()
        self._subscribers: dict[int, _Subscriber] = {}
        self._next_sub_id: int = 1

        # Snapshots — exposed via properties below. Keys:
        #   orders: orderId (int → str) → order dict
        #   positions: symbol → position dict
        #   account: dict (latest snapshot) | None
        self._orders: dict[str, dict[str, Any]] = {}
        self._positions: dict[str, dict[str, Any]] = {}
        self._account: dict[str, Any] | None = None

        # Per-day, per-account baseline + kill state. Loaded from journal
        # on the first account update of the day, persisted across
        # restarts. Keyed by account so switching IB accounts mid-day
        # doesn't trip the kill switch on a stale baseline.
        self._account_id: str = ""
        self._managed_accounts: list[str] = []
        self._date_et: str | None = None
        self._start_equity: float | None = None
        self._current_equity: float | None = None
        self._kill_active: bool = False
        self._kill_reason: str | None = None
        self._kill_tripped_at: str | None = None

        # Order metadata captured at submit time (planned_stop, score).
        self._pending_meta: dict[int, _OrderMeta] = {}

        # Cached previous payloads — used to dedupe identical broadcasts.
        self._last_risk_payload: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Subscriber fanout
    # ------------------------------------------------------------------

    def subscribe(
        self, loop: asyncio.AbstractEventLoop
    ) -> tuple[asyncio.Queue[dict[str, Any]], int]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        with self._lock:
            sub_id = self._next_sub_id
            self._next_sub_id += 1
            self._subscribers[sub_id] = _Subscriber(queue=q, loop=loop, id=sub_id)
        return q, sub_id

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subscribers.pop(sub_id, None)

    def _broadcast(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers.values())
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(_safe_put, sub.queue, event)
            except RuntimeError:
                # Loop closed; subscriber will be cleaned up on next event.
                pass

    # ------------------------------------------------------------------
    # Snapshot accessors (used by REST endpoints to seed the SSE client)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "account": dict(self._account) if self._account else None,
                "positions": [dict(p) for p in self._positions.values()],
                "orders": [dict(o) for o in self._orders.values()],
                "risk": self.risk_status_unlocked(),
                "accounts": list(self._managed_accounts),
                "default_account": self._account_id or None,
            }

    def set_accounts(self, default_account: str, managed_accounts: list[str]) -> None:
        """Called by IBBroker after `connectAsync` populates the managed-
        account list. The default account is the one risk metrics are
        tracked against; the others are valid order-routing targets but
        their drawdown isn't separately tracked (Phase 1).
        """
        with self._lock:
            changed = (
                self._account_id != default_account
                or self._managed_accounts != managed_accounts
            )
            self._account_id = default_account or ""
            self._managed_accounts = list(managed_accounts)
        if changed:
            self._broadcast({
                "kind": "accounts",
                "payload": {
                    "default_account": default_account or None,
                    "accounts": list(managed_accounts),
                },
            })

    def risk_status_unlocked(self) -> dict[str, Any]:
        # Caller is expected to hold the lock OR be OK with a slightly
        # stale read (this only constructs from immutable scalars).
        dd_pct = self._drawdown_pct()
        return {
            "date_et": self._date_et,
            "account": self._account_id or None,
            "start_equity": self._start_equity,
            "current_equity": self._current_equity,
            "drawdown_pct": dd_pct,
            "limit_pct": self.max_drawdown_pct,
            "kill_active": self._kill_active,
            "kill_reason": self._kill_reason,
            "kill_tripped_at": self._kill_tripped_at,
            "enabled": self.max_drawdown_pct > 0,
        }

    def risk_status(self) -> dict[str, Any]:
        with self._lock:
            return self.risk_status_unlocked()

    @property
    def kill_active(self) -> bool:
        return self._kill_active

    def _drawdown_pct(self) -> float | None:
        if self._start_equity is None or self._current_equity is None:
            return None
        if self._start_equity <= 0:
            return None
        return (self._current_equity - self._start_equity) / self._start_equity * 100.0

    # ------------------------------------------------------------------
    # Risk / kill switch
    # ------------------------------------------------------------------

    def reset_kill(self) -> None:
        date_et = self._date_et or today_et()
        self.journal.reset_kill(date_et, self._account_id)
        with self._lock:
            self._kill_active = False
            self._kill_reason = None
            self._kill_tripped_at = None
        self._broadcast({"kind": "risk", "payload": self.risk_status()})

    def load_today(self, current_equity: float) -> None:
        """Initialize per-day baseline from journal (or seed it). Called
        the first time we see a NetLiquidation value today, scoped to
        the current account so switching accounts mid-day doesn't reuse
        a stale baseline.
        """
        date_et = today_et()
        sess = self.journal.get_or_init_session(
            date_et, current_equity, self._account_id
        )
        with self._lock:
            self._date_et = date_et
            self._start_equity = float(sess["start_equity"])
            self._kill_active = bool(sess["kill_active"])
            self._kill_reason = sess.get("kill_reason")
            self._kill_tripped_at = sess.get("kill_tripped_at")

    def _maybe_trip_kill(self) -> bool:
        """Return True if this call newly tripped the kill switch."""
        if self.max_drawdown_pct <= 0 or self._kill_active:
            return False
        dd = self._drawdown_pct()
        if dd is None:
            return False
        if dd <= -self.max_drawdown_pct:
            reason = (
                f"Daily drawdown {dd:.2f}% breached limit "
                f"-{self.max_drawdown_pct:.2f}%"
            )
            ts = datetime.now(timezone.utc).isoformat()
            date_et = self._date_et or today_et()
            self.journal.trip_kill(date_et, reason, self._account_id)
            self._kill_active = True
            self._kill_reason = reason
            self._kill_tripped_at = ts
            log.warning("Kill switch tripped: %s", reason)
            return True
        return False

    # ------------------------------------------------------------------
    # ib_async event handlers (called on the broker's loop thread)
    # ------------------------------------------------------------------

    def remember_order_meta(
        self,
        order_id: int,
        *,
        planned_stop: float | None,
        score_at_entry: float | None,
    ) -> None:
        if order_id <= 0:
            return
        with self._lock:
            if len(self._pending_meta) >= _MAX_PENDING_META:
                oldest = min(self._pending_meta.items(), key=lambda kv: kv[1].submitted_at)
                self._pending_meta.pop(oldest[0], None)
            self._pending_meta[order_id] = _OrderMeta(
                planned_stop=planned_stop,
                score_at_entry=score_at_entry,
            )

    def on_order_status(self, order_dict: dict[str, Any]) -> None:
        key = str(order_dict.get("id") or "")
        if not key:
            return
        with self._lock:
            self._orders[key] = order_dict
        self._broadcast({"kind": "order", "payload": order_dict})

    def on_position_update(self, pos_dict: dict[str, Any]) -> None:
        sym = str(pos_dict.get("symbol") or "")
        if not sym:
            return
        with self._lock:
            if pos_dict.get("qty", 0) == 0:
                self._positions.pop(sym, None)
            else:
                self._positions[sym] = pos_dict
        self._broadcast({"kind": "position", "payload": pos_dict})

    def on_account_update(self, account: dict[str, Any]) -> None:
        equity = account.get("equity")
        # Only treat positive NetLiquidation as a real value. Account
        # value events arrive per-tag; until NetLiquidation lands, our
        # aggregated snapshot reports equity=0, and pinning the session
        # baseline at 0 would make drawdown_pct meaningless forever.
        valid = isinstance(equity, (int, float)) and float(equity) > 0
        with self._lock:
            # Per-tag accountValueEvent fires often with identical
            # aggregated snapshots; dedupe so the SSE stream isn't
            # noise-filled.
            account_changed = self._account != account
            self._account = account
            if valid:
                self._current_equity = float(equity)
        if valid and self._start_equity is None:
            # Initialize the session row outside the lock — it touches sqlite.
            self.load_today(float(equity))
        # Self-heal a bad baseline persisted earlier (start_equity == 0
        # from a pre-NetLiquidation snapshot). Reseed once we see a real
        # equity value.
        if valid and self._start_equity is not None and self._start_equity <= 0:
            self._reseed_session(float(equity))
        with self._lock:
            tripped = self._maybe_trip_kill()
            risk_payload = self.risk_status_unlocked()
            risk_changed = self._last_risk_payload != risk_payload
            self._last_risk_payload = risk_payload
        if account_changed:
            self._broadcast({"kind": "account", "payload": account})
        if risk_changed:
            self._broadcast({"kind": "risk", "payload": risk_payload})
        if tripped:
            self._broadcast({"kind": "kill_tripped", "payload": risk_payload})

    def _reseed_session(self, equity: float) -> None:
        date_et = self._date_et or today_et()
        try:
            self.journal.reseed_session_start(date_et, equity, self._account_id)
        except Exception:
            log.exception("Reseed session start failed")
            return
        with self._lock:
            self._start_equity = equity

    def on_fill(
        self,
        *,
        exec_id: str,
        ts: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        commission: float,
        order_id: int,
        order_perm_id: str | None,
    ) -> None:
        """Persist a fill (with its captured score/stop) and emit deltas.

        Called from `commissionReportEvent` so we have the final commission
        in hand. `record_fill` is idempotent on `exec_id`, so duplicate
        events are harmless.
        """
        with self._lock:
            meta = self._pending_meta.get(order_id)
        try:
            closed_trade = self.journal.record_fill(
                exec_id=exec_id,
                ts=ts,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                commission=commission,
                order_perm_id=order_perm_id,
                score_at_entry=meta.score_at_entry if meta else None,
                planned_stop=meta.planned_stop if meta else None,
            )
        except Exception:
            log.exception("Journal record_fill failed for exec=%s", exec_id)
            return
        self._broadcast({
            "kind": "fill",
            "payload": {
                "exec_id": exec_id,
                "ts": ts,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "commission": commission,
            },
        })
        if closed_trade is not None:
            self._broadcast({"kind": "trade_closed", "payload": closed_trade})


def _safe_put(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        # Slow consumer — drop oldest so we keep moving.
        try:
            queue.get_nowait()
            queue.put_nowait(event)
        except Exception:
            pass
