"""Interactive Brokers broker via ib_async.

Mirrors the previous Alpaca shape (`AccountSnapshot`, `Position`,
`OrderRecord`, plus methods for get_account / get_positions /
submit_order / close_position / close_all_positions / get_orders) so the
FastAPI surface and the React UI don't change. Implementation talks to a
running TWS or IB Gateway, which is responsible for routing orders.

Concurrency model is borrowed from `scanner.data.ib_provider`: every
ib_async call runs on a dedicated asyncio loop hosted on its own thread,
because the underlying socket reader task is bound to whichever loop the
client was constructed on. FastAPI's threadpool dispatches into here from
arbitrary threads, so we never call `ib.run()` on the caller's thread.

Event-driven state: at connect time we subscribe to ib_async's
`orderStatusEvent`, `execDetailsEvent`, `commissionReportEvent`,
`updateAccountValueEvent`, and `updatePortfolioEvent`. Each handler
funnels into a `BrokerState` instance which keeps an in-process
snapshot of orders/positions/account, persists fills to a SQLite
journal, computes the daily-drawdown circuit breaker, and fans events
out to SSE subscribers.

Connection settings come from env vars at construction time, distinct
from the data-provider env so the two can hit different ports / client
IDs without colliding (the data provider uses readonly=True; the broker
needs readonly=False to place orders):

  - IB_BROKER_HOST           (default 127.0.0.1)
  - IB_BROKER_PORT           (default 7497 — TWS Paper)
  - IB_BROKER_CLIENT_ID      (default 28 — distinct from the data provider's 27)
  - IB_BROKER_ACCOUNT        (optional; if set, used for order routing and
                              account queries; otherwise the first account
                              returned by ib.managedAccounts() is used)
  - MAX_DAILY_DRAWDOWN_PCT   (default 5.0 — set 0 to disable the breaker)
  - BULLISH_CACHE_DIR        (default ./data_cache — journal.sqlite lives here)

`paper` is detected from the account ID itself — IBKR paper accounts
always start with "DU"; live accounts start with "U" (or other letters
for sub-accounts). No env flag needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scanner.broker.journal import Journal
from scanner.broker.state import BrokerState

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from ib_async import IB  # pragma: no cover
else:
    IB = Any


class IBNotConfigured(RuntimeError):
    """Raised by get_broker() if env vars indicate IB is intentionally off."""


class KillSwitchActive(RuntimeError):
    """Raised by submit_order / close_position when the daily-drawdown
    circuit breaker has tripped. Callers map this to HTTP 423.
    """


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    last_equity: float
    cash: float
    buying_power: float
    portfolio_value: float
    pnl_today_abs: float
    pnl_today_pct: float
    paper: bool
    status: str  # "ACTIVE" / "ACCOUNT_NOT_FOUND" / etc.

    def to_dict(self) -> dict[str, Any]:
        return {
            "equity": round(self.equity, 2),
            "last_equity": round(self.last_equity, 2),
            "cash": round(self.cash, 2),
            "buying_power": round(self.buying_power, 2),
            "portfolio_value": round(self.portfolio_value, 2),
            "pnl_today_abs": round(self.pnl_today_abs, 2),
            "pnl_today_pct": round(self.pnl_today_pct, 4),
            "paper": self.paper,
            "status": self.status,
        }


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    cost_basis: float
    unrealized_pl_abs: float
    unrealized_pl_pct: float
    side: str  # "long" / "short"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "avg_entry_price": round(self.avg_entry_price, 4),
            "current_price": round(self.current_price, 4),
            "market_value": round(self.market_value, 2),
            "cost_basis": round(self.cost_basis, 2),
            "unrealized_pl_abs": round(self.unrealized_pl_abs, 2),
            "unrealized_pl_pct": round(self.unrealized_pl_pct, 4),
            "side": self.side,
        }


@dataclass(frozen=True)
class OrderRecord:
    """Lightweight view of an IB order/trade, suitable for UI display."""

    id: str
    symbol: str
    side: str
    qty: float
    filled_qty: float
    type: str
    time_in_force: str
    limit_price: float | None
    status: str
    submitted_at: str | None
    filled_at: str | None
    filled_avg_price: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "filled_qty": self.filled_qty,
            "type": self.type,
            "time_in_force": self.time_in_force,
            "limit_price": (
                round(self.limit_price, 4) if self.limit_price is not None else None
            ),
            "status": self.status,
            "submitted_at": self.submitted_at,
            "filled_at": self.filled_at,
            "filled_avg_price": (
                round(self.filled_avg_price, 4)
                if self.filled_avg_price is not None
                else None
            ),
        }


@dataclass
class IBBrokerConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 28
    account: str | None = None
    connect_timeout_sec: float = 10.0
    max_daily_drawdown_pct: float = 5.0
    journal_path: Path | None = None
    # Force paper-mode UI regardless of account ID prefix. Useful for
    # paper sub-accounts whose IDs don't follow the "DU"/"DF" convention,
    # or any environment where the user wants the cautious badge.
    force_paper: bool = False

    @classmethod
    def from_env(cls) -> IBBrokerConfig:
        acct = os.environ.get("IB_BROKER_ACCOUNT", "").strip() or None
        cache_root = Path(
            os.environ.get(
                "BULLISH_CACHE_DIR",
                str(Path(__file__).resolve().parent.parent.parent / "data_cache"),
            )
        )
        force_paper = os.environ.get("IB_BROKER_PAPER", "").strip().lower() in (
            "1", "true", "yes",
        )
        return cls(
            host=os.environ.get("IB_BROKER_HOST", "127.0.0.1"),
            port=int(os.environ.get("IB_BROKER_PORT", "7497")),
            client_id=int(os.environ.get("IB_BROKER_CLIENT_ID", "28")),
            account=acct,
            max_daily_drawdown_pct=float(
                os.environ.get("MAX_DAILY_DRAWDOWN_PCT", "5.0")
            ),
            journal_path=cache_root / "journal.sqlite",
            force_paper=force_paper,
        )


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _maybe_f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return str(iso())
    return str(value)


class IBBroker:
    """Read account/positions/orders and place + close orders via TWS/IB Gateway."""

    def __init__(
        self,
        config: IBBrokerConfig | None = None,
        *,
        journal: Journal | None = None,
        state: BrokerState | None = None,
    ) -> None:
        self._cfg = config or IBBrokerConfig.from_env()
        self._ib: IB | None = None
        self._account: str | None = self._cfg.account
        self._managed_accounts: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        self._events_wired: bool = False
        # Latest values per IB account-summary tag — accumulated as
        # accountValueEvent fires so we can emit consolidated snapshots.
        self._account_tags: dict[str, str] = {}

        if journal is None:
            jp = self._cfg.journal_path or (
                Path(__file__).resolve().parent.parent.parent
                / "data_cache" / "journal.sqlite"
            )
            journal = Journal(jp)
        self.journal: Journal = journal
        self.state: BrokerState = state or BrokerState(
            journal, max_drawdown_pct=self._cfg.max_daily_drawdown_pct
        )

    # ------------------------------------------------------------------
    # Loop thread plumbing — same shape as IBProvider.
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

            t = threading.Thread(target=_run, name="ib-broker-loop", daemon=True)
            t.start()
            ready.wait()
            self._loop = holder[0]
            self._thread = t
            return self._loop

    def _submit(self, coro: Any, timeout: float | None = 30.0) -> Any:
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    async def _async_connect(self) -> Any:
        import ib_async

        if self._ib is not None and self._ib.isConnected():
            return self._ib
        self._ib = None
        self._events_wired = False

        ib: Any = ib_async.IB()
        log.info(
            "IB broker connecting to %s:%d clientId=%d",
            self._cfg.host, self._cfg.port, self._cfg.client_id,
        )
        await ib.connectAsync(
            host=self._cfg.host,
            port=self._cfg.port,
            clientId=self._cfg.client_id,
            timeout=self._cfg.connect_timeout_sec,
            readonly=False,
        )
        self._ib = ib

        # Pick the default account to use for orders. IB returns BOTH
        # advisor master (DF/FU) and direct (DU/U) accounts via
        # `managedAccounts()`. Master accounts can't take orders
        # directly — only their sub-accounts can — so we filter them out
        # of the tradeable list before exposing it to the dashboard.
        all_managed = list(ib.managedAccounts() or [])
        masters = [a for a in all_managed if self._is_advisor_master(a)]
        tradeable = [a for a in all_managed if not self._is_advisor_master(a)]
        if masters:
            log.info(
                "IB returned advisor master account(s) %s; hiding from dropdown",
                masters,
            )
        self._managed_accounts = tradeable
        if not all_managed:
            log.warning("IB broker connected but no managed accounts returned")
        elif not tradeable:
            log.warning(
                "Connection only exposes advisor master account(s) %s — "
                "no tradeable sub-accounts visible. Orders will fail until a "
                "tradeable account is added to the FA configuration.",
                all_managed,
            )
        elif self._account is None:
            self._account = tradeable[0]
        elif self._account not in tradeable:
            log.warning(
                "IB_BROKER_ACCOUNT=%r not in tradeable accounts %s; using first",
                self._account, tradeable,
            )
            self._account = tradeable[0]
        # Tell the state cache about the default account so the daily-DD
        # session row is keyed correctly. Also broadcasts an `accounts`
        # SSE event so the dashboard's dropdown refreshes.
        self.state.set_accounts(self._account or "", tradeable)

        # Pull any open orders from TWS so trades() reflects orders placed
        # in the GUI or by other clients, not just this session.
        try:
            await ib.reqAllOpenOrdersAsync()
        except Exception:
            log.debug("reqAllOpenOrdersAsync failed (non-fatal)", exc_info=True)

        # Subscribe to streaming account values + portfolio so the state
        # cache is kept fresh by IB push, not by polling.
        if self._account:
            try:
                ib.reqAccountUpdates(True, self._account)
            except Exception:
                log.debug("reqAccountUpdates failed (non-fatal)", exc_info=True)

        self._wire_events(ib)
        return ib

    def _wire_events(self, ib: Any) -> None:
        if self._events_wired:
            return
        # Order lifecycle: status, exec, commission.
        ib.orderStatusEvent += self._on_order_status_event
        ib.execDetailsEvent += self._on_exec_details_event
        ib.commissionReportEvent += self._on_commission_report_event
        # Streaming portfolio + account values come from reqAccountUpdates.
        ib.updatePortfolioEvent += self._on_update_portfolio_event
        ib.accountValueEvent += self._on_account_value_event
        self._events_wired = True

    # ------------------------------------------------------------------
    # Event handlers (run on the broker's loop thread)
    # ------------------------------------------------------------------

    def _on_order_status_event(self, trade: Any) -> None:
        try:
            rec = _trade_to_record(trade)
            self.state.on_order_status(rec.to_dict())
        except Exception:
            log.exception("orderStatusEvent handler failed")

    def _on_exec_details_event(self, trade: Any, fill: Any) -> None:
        # We persist on commissionReportEvent (which carries the final
        # commission), but also push an order-status delta here so the UI
        # reflects the fill immediately.
        try:
            rec = _trade_to_record(trade)
            self.state.on_order_status(rec.to_dict())
        except Exception:
            log.exception("execDetailsEvent handler failed")

    def _on_commission_report_event(
        self, trade: Any, fill: Any, report: Any
    ) -> None:
        try:
            execution = getattr(fill, "execution", None)
            contract = getattr(fill, "contract", None) or getattr(trade, "contract", None)
            order = getattr(trade, "order", None)
            exec_id = str(getattr(execution, "execId", "") or "")
            if not exec_id:
                return
            symbol = str(getattr(contract, "symbol", "") or "").upper()
            side_raw = str(getattr(execution, "side", "") or "").upper()
            # IB execution side: "BOT" / "SLD".
            side = "buy" if side_raw == "BOT" else "sell" if side_raw == "SLD" else side_raw.lower()
            qty = _f(getattr(execution, "shares", 0))
            price = _f(getattr(execution, "price", 0))
            commission = _f(getattr(report, "commission", 0))
            ts_raw = getattr(execution, "time", None)
            ts = _iso(ts_raw) or datetime.now(timezone.utc).isoformat()
            order_id = int(getattr(order, "orderId", 0) or 0)
            perm_id = getattr(order, "permId", None)
            self.state.on_fill(
                exec_id=exec_id,
                ts=ts,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                commission=commission,
                order_id=order_id,
                order_perm_id=str(perm_id) if perm_id else None,
            )
            # Refresh the order record now that the fill is fully reconciled.
            rec = _trade_to_record(trade)
            self.state.on_order_status(rec.to_dict())
        except Exception:
            log.exception("commissionReportEvent handler failed")

    def _on_update_portfolio_event(self, item: Any) -> None:
        try:
            contract = getattr(item, "contract", None)
            qty = _f(getattr(item, "position", 0))
            avg = _f(getattr(item, "averageCost", 0))
            mkt = _f(getattr(item, "marketPrice", avg))
            mkt_value = _f(getattr(item, "marketValue", qty * mkt))
            cost_basis = qty * avg
            upl_abs = _f(getattr(item, "unrealizedPNL", mkt_value - cost_basis))
            upl_pct = (upl_abs / cost_basis * 100.0) if cost_basis else 0.0
            symbol = str(getattr(contract, "symbol", "") or "").upper()
            payload = {
                "symbol": symbol,
                "qty": qty,
                "avg_entry_price": round(avg, 4),
                "current_price": round(mkt, 4),
                "market_value": round(mkt_value, 2),
                "cost_basis": round(cost_basis, 2),
                "unrealized_pl_abs": round(upl_abs, 2),
                "unrealized_pl_pct": round(upl_pct, 4),
                "side": "long" if qty > 0 else "short" if qty < 0 else "flat",
            }
            self.state.on_position_update(payload)
        except Exception:
            log.exception("updatePortfolioEvent handler failed")

    def _on_account_value_event(self, av: Any) -> None:
        try:
            tag = str(getattr(av, "tag", "") or "")
            value = str(getattr(av, "value", "") or "")
            currency = str(getattr(av, "currency", "") or "")
            # IB sends tags in many currencies; only the base/aggregate
            # ones (currency == "" or "BASE") roll up to NetLiquidation.
            if currency and currency not in ("BASE", "USD"):
                return
            self._account_tags[tag] = value
            if tag in (
                "NetLiquidation",
                "TotalCashValue",
                "BuyingPower",
                "AvailableFunds",
                "GrossPositionValue",
                "RealizedPnL",
                "UnrealizedPnL",
            ):
                snap = self._snapshot_from_tags()
                self.state.on_account_update(snap)
        except Exception:
            log.exception("accountValueEvent handler failed")

    def _snapshot_from_tags(self) -> dict[str, Any]:
        t = self._account_tags
        equity = _f(t.get("NetLiquidation"))
        cash = _f(t.get("TotalCashValue"))
        buying_power = _f(t.get("BuyingPower") or t.get("AvailableFunds"))
        gross = _f(t.get("GrossPositionValue"))
        portfolio_value = gross if gross > 0 else equity
        realized = _f(t.get("RealizedPnL"))
        unrealized = _f(t.get("UnrealizedPnL"))
        pnl_today_abs = realized + unrealized
        last_equity = equity - pnl_today_abs
        pnl_today_pct = (
            (pnl_today_abs / last_equity * 100.0) if last_equity > 0 else 0.0
        )
        return {
            "equity": round(equity, 2),
            "last_equity": round(last_equity, 2),
            "cash": round(cash, 2),
            "buying_power": round(buying_power, 2),
            "portfolio_value": round(portfolio_value, 2),
            "pnl_today_abs": round(pnl_today_abs, 2),
            "pnl_today_pct": round(pnl_today_pct, 4),
            "paper": self.paper,
            "status": "ACTIVE" if equity > 0 or cash > 0 else "EMPTY",
        }

    async def _async_disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
        self._ib = None

    def disconnect(self) -> None:
        with self._loop_lock:
            loop = self._loop
            thread = self._thread
        if loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._async_disconnect(), loop)
            fut.result(timeout=5)
        except Exception:
            log.debug("IB broker disconnect raised", exc_info=True)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        with self._loop_lock:
            self._loop = None
            self._thread = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # IBKR account ID prefixes:
    #   - DU = paper individual          (tradeable)
    #   - DF = paper FA / family master  (NOT tradeable — manages DU sub-accounts)
    #   - U  = live individual           (tradeable)
    #   - FU = live FA / family master   (NOT tradeable — manages U sub-accounts)
    # `paper` mode is determined by the *connection* (any DU or DF means
    # this TWS is paper), but order routing must skip the master accounts
    # since IB rejects orders sent to a DF/FU directly — orders have to
    # target one of the underlying sub-accounts.
    _PAPER_PREFIXES: tuple[str, ...] = ("DU", "DF")
    _ADVISOR_MASTER_PREFIXES: tuple[str, ...] = ("DF", "FU")

    @property
    def paper(self) -> bool:
        if self._cfg.force_paper:
            return True
        acct = self._account or ""
        if not acct:
            # If we can't tell yet, assume paper for safety so the UI shows
            # the cautious badge until a real connection populates it.
            return True
        # Any tradeable or master account in the paper space implies the
        # whole connection is paper.
        return acct.upper().startswith(self._PAPER_PREFIXES)

    @classmethod
    def _is_advisor_master(cls, account_id: str) -> bool:
        return account_id.upper().startswith(cls._ADVISOR_MASTER_PREFIXES)

    @property
    def account(self) -> str | None:
        return self._account

    @property
    def accounts(self) -> list[str]:
        """All managed accounts visible from this connection. Populated
        at connect; empty before the first call to a broker method.
        """
        return list(self._managed_accounts)

    @property
    def default_account(self) -> str | None:
        return self._account

    def get_account(self) -> AccountSnapshot:
        return self._submit(self._async_get_account())

    def get_positions(self) -> list[Position]:
        return self._submit(self._async_get_positions())

    def _resolve_account(self, account: str | None) -> str | None:
        """Pick the account for an order. None → broker's default. A
        non-default account is validated against the managed list so
        we don't silently route to whatever IB happens to accept.
        """
        if account is None or account == "":
            return self._account
        if self._managed_accounts and account not in self._managed_accounts:
            raise ValueError(
                f"Account {account!r} is not in managed accounts "
                f"{self._managed_accounts}"
            )
        return account

    def submit_order(
        self,
        *,
        symbol: str,
        qty: float,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
        limit_price: float | None = None,
        client_order_id: str | None = None,  # noqa: ARG002 — IB has no equivalent
        planned_stop: float | None = None,
        score_at_entry: float | None = None,
        peg_offset: float | None = None,
        cap_price: float | None = None,
        account: str | None = None,
    ) -> OrderRecord:
        if self.state.kill_active:
            raise KillSwitchActive(
                "Daily-drawdown circuit breaker is active. "
                "Reset via POST /broker/risk-reset to resume trading."
            )
        return self._submit(
            self._async_submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                order_type=order_type,
                time_in_force=time_in_force,
                limit_price=limit_price,
                planned_stop=planned_stop,
                score_at_entry=score_at_entry,
                peg_offset=peg_offset,
                cap_price=cap_price,
                account=account,
            )
        )

    def close_position(
        self,
        symbol: str,
        *,
        qty: float | None = None,
        percentage: float | None = None,
        account: str | None = None,
    ) -> OrderRecord:
        if qty is not None and percentage is not None:
            raise ValueError("Pass qty OR percentage, not both")
        if qty is not None and qty <= 0:
            raise ValueError("qty must be > 0")
        if percentage is not None and not 0 < percentage <= 100:
            raise ValueError("percentage must be in (0, 100]")
        if self.state.kill_active:
            raise KillSwitchActive(
                "Daily-drawdown circuit breaker is active. "
                "Reset via POST /broker/risk-reset to resume trading."
            )
        return self._submit(
            self._async_close_position(
                symbol=symbol, qty=qty, percentage=percentage, account=account,
            )
        )

    def close_all_positions(self, *, cancel_orders: bool = True) -> dict[str, Any]:
        return self._submit(self._async_close_all(cancel_orders=cancel_orders))

    def get_orders(self, *, limit: int = 30, status: str = "all") -> list[OrderRecord]:
        return self._submit(self._async_get_orders(limit=limit, status=status))

    # ------------------------------------------------------------------
    # Async implementations (run on the broker's loop thread)
    # ------------------------------------------------------------------

    async def _async_get_account(self) -> AccountSnapshot:
        ib = await self._async_connect()
        # ib_async's sync `accountSummary()` calls `util.run()` internally,
        # which deadlocks when invoked from inside our running coroutine
        # ("This event loop is already running"). The async variant just
        # awaits the underlying request — same data, no run().
        values = await ib.accountSummaryAsync(self._account or "")
        tags: dict[str, str] = {v.tag: v.value for v in (values or [])}

        equity = _f(tags.get("NetLiquidation"))
        cash = _f(tags.get("TotalCashValue"))
        buying_power = _f(tags.get("BuyingPower") or tags.get("AvailableFunds"))
        gross = _f(tags.get("GrossPositionValue"))
        portfolio_value = gross if gross > 0 else equity

        # IB doesn't expose a true "daily P&L" tag in accountSummary. We
        # approximate today's P&L as RealizedPnL + UnrealizedPnL (session
        # totals), which is what the UI shows under "P&L Today". For an
        # exact daily figure you'd need reqPnL(), but that subscribes to
        # a stream and can deadlock the event loop on first use here.
        realized = _f(tags.get("RealizedPnL"))
        unrealized = _f(tags.get("UnrealizedPnL"))
        pnl_today_abs = realized + unrealized

        last_equity = equity - pnl_today_abs
        pnl_today_pct = (
            (pnl_today_abs / last_equity * 100.0) if last_equity > 0 else 0.0
        )

        return AccountSnapshot(
            equity=equity,
            last_equity=last_equity,
            cash=cash,
            buying_power=buying_power,
            portfolio_value=portfolio_value,
            pnl_today_abs=pnl_today_abs,
            pnl_today_pct=pnl_today_pct,
            paper=self.paper,
            status="ACTIVE" if equity > 0 or cash > 0 else "EMPTY",
        )

    async def _async_get_positions(self) -> list[Position]:
        ib = await self._async_connect()
        rows = ib.positions(self._account or "")
        out: list[Position] = []
        for p in rows:
            qty = float(getattr(p, "position", 0))
            if qty == 0:
                continue
            avg = float(getattr(p, "avgCost", 0.0))
            contract = getattr(p, "contract", None)
            symbol = str(getattr(contract, "symbol", "") or "")
            # IB doesn't put current price on Position; fall back to
            # marketPrice from the portfolio view if available, else use
            # avg cost (best we can do without a quote subscription).
            current_price = avg
            try:
                portfolio = ib.portfolio(self._account or "")
                for item in portfolio:
                    if getattr(item, "contract", None) is contract or (
                        contract is not None
                        and getattr(item.contract, "conId", None)
                        == getattr(contract, "conId", None)
                    ):
                        current_price = float(getattr(item, "marketPrice", avg))
                        break
            except Exception:
                pass

            cost_basis = qty * avg
            market_value = qty * current_price
            upl_abs = market_value - cost_basis
            upl_pct = (upl_abs / cost_basis * 100.0) if cost_basis else 0.0
            out.append(
                Position(
                    symbol=symbol,
                    qty=qty,
                    avg_entry_price=avg,
                    current_price=current_price,
                    market_value=market_value,
                    cost_basis=cost_basis,
                    unrealized_pl_abs=upl_abs,
                    unrealized_pl_pct=upl_pct,
                    side="long" if qty > 0 else "short",
                )
            )
        return out

    async def _async_submit_order(
        self,
        *,
        symbol: str,
        qty: float,
        side: str,
        order_type: str,
        time_in_force: str,
        limit_price: float | None,
        planned_stop: float | None = None,
        score_at_entry: float | None = None,
        peg_offset: float | None = None,
        cap_price: float | None = None,
        account: str | None = None,
    ) -> OrderRecord:
        ib = await self._async_connect()
        target_account = self._resolve_account(account)
        from ib_async import LimitOrder, MarketOrder, Order, Stock

        action = side.upper()
        if action not in ("BUY", "SELL"):
            raise ValueError("side must be 'buy' or 'sell'")

        contract = Stock(symbol.upper(), "SMART", "USD")
        await ib.qualifyContractsAsync(contract)

        kind = order_type.lower()
        if kind == "market":
            order = MarketOrder(action, qty)
        elif kind == "limit":
            if limit_price is None or limit_price <= 0:
                raise ValueError("limit_price required for limit orders")
            order = LimitOrder(action, qty, limit_price)
        elif kind in ("pegprim", "peg_prim", "rel"):
            # IB Pegged-to-Primary (REL): the working price tracks the
            # primary exchange's best bid (BUY) or ask (SELL) plus/minus
            # `auxPrice` (the offset). `lmtPrice` is a hard cap (BUY) or
            # floor (SELL) so a runaway tape can't blow past your tolerance.
            if peg_offset is None or peg_offset <= 0:
                raise ValueError("peg_offset (>0) required for pegprim orders")
            if cap_price is None or cap_price <= 0:
                raise ValueError("cap_price (>0) required for pegprim orders")
            order = Order()
            order.action = action
            order.totalQuantity = qty
            order.orderType = "REL"
            order.auxPrice = float(peg_offset)
            order.lmtPrice = float(cap_price)
        else:
            raise ValueError(f"Unsupported order_type: {order_type!r}")

        order.tif = time_in_force.upper()
        if target_account:
            order.account = target_account

        trade = ib.placeOrder(contract, order)
        # Stash score / stop against the orderId so the
        # commissionReportEvent handler can persist them on the fill.
        order_id = int(getattr(trade.order, "orderId", 0) or 0)
        if order_id and (planned_stop is not None or score_at_entry is not None):
            self.state.remember_order_meta(
                order_id,
                planned_stop=planned_stop,
                score_at_entry=score_at_entry,
            )
        # Give IB a tick to populate orderStatus before we read it.
        for _ in range(10):
            await asyncio.sleep(0.1)
            if trade.orderStatus.status not in ("PendingSubmit", ""):
                break
        return _trade_to_record(trade)

    async def _async_close_position(
        self,
        *,
        symbol: str,
        qty: float | None,
        percentage: float | None,
        account: str | None = None,
    ) -> OrderRecord:
        ib = await self._async_connect()
        target_account = self._resolve_account(account)
        from ib_async import MarketOrder, Stock

        sym = symbol.upper()
        positions = ib.positions(target_account or "")
        target = None
        for p in positions:
            if str(getattr(p.contract, "symbol", "")).upper() == sym:
                target = p
                break
        if target is None:
            raise ValueError(f"No open position for {sym}")

        held = float(target.position)
        if held == 0:
            raise ValueError(f"Position for {sym} is flat")

        if qty is not None:
            close_qty = min(qty, abs(held))
        elif percentage is not None:
            close_qty = abs(held) * percentage / 100.0
            # IB stocks are whole-share — round to int and floor at 1.
            close_qty = max(1, int(close_qty))
        else:
            close_qty = abs(held)

        # Opposite side to flatten.
        action = "SELL" if held > 0 else "BUY"
        contract = Stock(sym, "SMART", "USD")
        await ib.qualifyContractsAsync(contract)
        order = MarketOrder(action, close_qty)
        order.tif = "DAY"
        if target_account:
            order.account = target_account

        trade = ib.placeOrder(contract, order)
        for _ in range(10):
            await asyncio.sleep(0.1)
            if trade.orderStatus.status not in ("PendingSubmit", ""):
                break
        return _trade_to_record(trade)

    async def _async_close_all(self, *, cancel_orders: bool) -> dict[str, Any]:
        ib = await self._async_connect()
        if cancel_orders:
            try:
                ib.reqGlobalCancel()
            except Exception:
                log.debug("reqGlobalCancel failed (non-fatal)", exc_info=True)
            # Give IB a beat to register the cancels.
            await asyncio.sleep(0.5)

        from ib_async import MarketOrder, Stock

        details: list[dict[str, Any]] = []
        ok = 0
        failed = 0
        positions = ib.positions(self._account or "")
        for p in positions:
            held = float(p.position)
            if held == 0:
                continue
            sym = str(getattr(p.contract, "symbol", "")).upper()
            try:
                contract = Stock(sym, "SMART", "USD")
                await ib.qualifyContractsAsync(contract)
                action = "SELL" if held > 0 else "BUY"
                order = MarketOrder(action, abs(held))
                order.tif = "DAY"
                if self._account:
                    order.account = self._account
                ib.placeOrder(contract, order)
                ok += 1
                details.append({"symbol": sym, "status": 200, "ok": True})
            except Exception as exc:
                failed += 1
                details.append(
                    {"symbol": sym, "status": 500, "ok": False, "error": repr(exc)}
                )
        return {
            "submitted": len(details),
            "ok": ok,
            "failed": failed,
            "details": details,
        }

    async def _async_get_orders(
        self, *, limit: int, status: str
    ) -> list[OrderRecord]:
        ib = await self._async_connect()
        # Refresh open-orders view from TWS so anything placed in the GUI
        # also surfaces here.
        try:
            await ib.reqAllOpenOrdersAsync()
        except Exception:
            log.debug("reqAllOpenOrdersAsync failed (non-fatal)", exc_info=True)
        trades = list(ib.trades() or [])
        trades.reverse()  # most recent first
        records = [_trade_to_record(t) for t in trades]
        if status.lower() != "all":
            records = [r for r in records if r.status == status.lower()]
        return records[:limit]


def _trade_to_record(trade: Any) -> OrderRecord:
    """Map an ib_async Trade -> OrderRecord."""
    contract = getattr(trade, "contract", None)
    order = getattr(trade, "order", None)
    status = getattr(trade, "orderStatus", None)
    log_entries = list(getattr(trade, "log", []) or [])

    submitted_at = log_entries[0].time if log_entries else None
    filled_at: Any = None
    for entry in log_entries:
        s = str(getattr(entry, "status", "")).lower()
        if s == "filled":
            filled_at = entry.time
            break

    raw_status = str(getattr(status, "status", "") or "").lower()
    # Translate IB's status vocabulary into the same set the UI expects
    # (filled / accepted / canceled / rejected / submitted / pending_*).
    mapped_status = {
        "filled": "filled",
        "cancelled": "canceled",
        "apicancelled": "canceled",
        "presubmitted": "accepted",
        "submitted": "accepted",
        "pendingsubmit": "pending",
        "pendingcancel": "pending",
        "inactive": "rejected",
        # Transient pre-flight state IB emits while validating an order;
        # the next status update flips it to accepted/submitted. Mapping
        # to "pending" keeps the UI from flashing red.
        "validationerror": "pending",
    }.get(raw_status, raw_status or "pending")

    order_type = str(getattr(order, "orderType", "") or "").lower()
    tif = str(getattr(order, "tif", "") or "").lower()
    side = str(getattr(order, "action", "") or "").lower()
    qty = _f(getattr(order, "totalQuantity", 0))
    filled_qty = _f(getattr(status, "filled", 0))
    avg_fill = _maybe_f(getattr(status, "avgFillPrice", None))
    limit_price = _maybe_f(getattr(order, "lmtPrice", None))
    # IB uses sys.float_info.max (≈1.79e308) as the sentinel for "no limit
    # price" on market orders. Treat that as None too.
    if limit_price is None or limit_price == 0.0 or limit_price > 1e300:
        limit_price = None

    # orderId is the stable per-session id; permId only gets assigned
    # after IB acknowledges the order, so keying by permId would mean the
    # same order shows up under two different ids during its first
    # lifecycle events. Use orderId for `id` and surface permId
    # separately if needed.
    order_id = getattr(order, "orderId", 0) or 0
    perm_id = getattr(order, "permId", 0) or 0
    stable_id = str(order_id) if order_id else (str(perm_id) if perm_id else "")
    return OrderRecord(
        id=stable_id,
        symbol=str(getattr(contract, "symbol", "") or ""),
        side=side,
        qty=qty,
        filled_qty=filled_qty,
        type=order_type,
        time_in_force=tif,
        limit_price=limit_price,
        status=mapped_status,
        submitted_at=_iso(submitted_at),
        filled_at=_iso(filled_at),
        filled_avg_price=avg_fill,
    )


def get_broker() -> IBBroker | None:
    """Build an `IBBroker` from env, or return None if explicitly disabled.

    Set IB_BROKER_DISABLED=1 to skip IB entirely (useful for environments
    without a TWS instance — the rest of the app still works). Otherwise
    we always return an `IBBroker`; actual connection is lazy on first
    method call so misconfiguration fails clearly at request time, not at
    server boot.
    """
    if os.environ.get("IB_BROKER_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None
    try:
        return IBBroker()
    except Exception as exc:
        log.warning("IB broker init failed: %s", exc)
        return None
