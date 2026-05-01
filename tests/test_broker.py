"""Tests for the IBKR broker wrapper.

ib_async is mocked at the module level so the tests run with no TWS and
no real socket. We override the IBBroker._async_* coroutines on a
subclass to feed pre-canned data — that way the public API surface
(`get_account`, `get_positions`, `submit_order`, etc.) and the loop
plumbing are exercised without a live connection.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

from scanner.broker.ib_broker import (
    AccountSnapshot,
    IBBroker,
    IBBrokerConfig,
    OrderRecord,
    Position,
    _trade_to_record,
    get_broker,
)


class _StubBroker(IBBroker):
    """Replaces _async_* with synchronous stubs so the loop thread runs them."""

    def __init__(self, *, account: str = "DU1234567") -> None:
        super().__init__(IBBrokerConfig(account=account))
        # Skip real connection; just record that the account is set.
        self._account = account

    async def _async_connect(self) -> Any:
        # Pretend we have a connected ib client. Returning a minimal
        # object is enough since the rest of our async impls are
        # overridden below.
        class _IB:
            def isConnected(self) -> bool:
                return True

        self._ib = _IB()  # type: ignore[assignment]
        return self._ib

    async def _async_get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            equity=10500.0,
            last_equity=10000.0,
            cash=2500.0,
            buying_power=20000.0,
            portfolio_value=10500.0,
            pnl_today_abs=500.0,
            pnl_today_pct=5.0,
            paper=self.paper,
            status="ACTIVE",
        )

    async def _async_get_positions(self) -> list[Position]:
        return [
            Position(
                symbol="AAPL",
                qty=10,
                avg_entry_price=150.0,
                current_price=155.0,
                market_value=1550.0,
                cost_basis=1500.0,
                unrealized_pl_abs=50.0,
                unrealized_pl_pct=(50.0 / 1500.0) * 100.0,
                side="long",
            ),
        ]

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
        if order_type.lower() == "limit" and (limit_price is None or limit_price <= 0):
            raise ValueError("limit_price required for limit orders")
        if order_type.lower() not in ("market", "limit"):
            raise ValueError(f"Unsupported order_type: {order_type!r}")
        if side.lower() not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        return OrderRecord(
            id="ord-1",
            symbol=symbol.upper(),
            side=side.lower(),
            qty=float(qty),
            filled_qty=0.0,
            type=order_type.lower(),
            time_in_force=time_in_force.lower(),
            limit_price=limit_price,
            status="accepted",
            submitted_at=None,
            filled_at=None,
            filled_avg_price=None,
        )

    async def _async_close_position(
        self,
        *,
        symbol: str,
        qty: float | None,
        percentage: float | None,
        account: str | None = None,
    ) -> OrderRecord:
        return OrderRecord(
            id="ord-close",
            symbol=symbol.upper(),
            side="sell",
            qty=qty if qty is not None else 0.0,
            filled_qty=0.0,
            type="market",
            time_in_force="day",
            limit_price=None,
            status="accepted",
            submitted_at=None,
            filled_at=None,
            filled_avg_price=None,
        )

    async def _async_close_all(self, *, cancel_orders: bool) -> dict[str, Any]:
        return {
            "submitted": 1,
            "ok": 1,
            "failed": 0,
            "details": [{"symbol": "AAPL", "status": 200, "ok": True}],
        }

    async def _async_get_orders(
        self, *, limit: int, status: str
    ) -> list[OrderRecord]:
        rows = [
            OrderRecord(
                id="ord-a",
                symbol="AAPL",
                side="buy",
                qty=10,
                filled_qty=10,
                type="market",
                time_in_force="day",
                limit_price=None,
                status="filled",
                submitted_at=None,
                filled_at=None,
                filled_avg_price=151.0,
            ),
            OrderRecord(
                id="ord-b",
                symbol="NVDA",
                side="sell",
                qty=5,
                filled_qty=0,
                type="market",
                time_in_force="day",
                limit_price=None,
                status="canceled",
                submitted_at=None,
                filled_at=None,
                filled_avg_price=None,
            ),
        ]
        if status.lower() != "all":
            rows = [r for r in rows if r.status == status.lower()]
        return rows[:limit]


@pytest.fixture
def broker() -> IBBroker:
    return _StubBroker()


@pytest.fixture
def live_broker() -> IBBroker:
    return _StubBroker(account="U7654321")


def test_get_account(broker: IBBroker) -> None:
    snap = broker.get_account()
    assert isinstance(snap, AccountSnapshot)
    assert snap.equity == 10500.0
    assert snap.last_equity == 10000.0
    assert snap.pnl_today_abs == 500.0
    assert snap.pnl_today_pct == pytest.approx(5.0, abs=1e-6)
    assert snap.paper is True
    assert snap.status == "ACTIVE"


def test_get_positions(broker: IBBroker) -> None:
    positions = broker.get_positions()
    assert len(positions) == 1
    aapl = positions[0]
    assert isinstance(aapl, Position)
    assert aapl.symbol == "AAPL"
    assert aapl.unrealized_pl_pct == pytest.approx(
        (5.0 / 150.0) * 100.0, abs=1e-6
    )


def test_paper_detection_du_prefix(broker: IBBroker) -> None:
    assert broker.paper is True
    assert broker.account == "DU1234567"


def test_paper_detection_df_prefix() -> None:
    """FA paper sub-accounts use the DF prefix (e.g. DFN394316). Earlier
    versions only matched DU and mis-classified these as live."""
    b = _StubBroker(account="DFN394316")
    assert b.paper is True


def test_paper_detection_live_account(live_broker: IBBroker) -> None:
    assert live_broker.paper is False
    assert live_broker.account == "U7654321"


def test_paper_force_env_override() -> None:
    """IB_BROKER_PAPER=1 forces paper mode regardless of account ID."""
    cfg = IBBrokerConfig(account="U7654321", force_paper=True)
    b = _StubBroker.__new__(_StubBroker)
    IBBroker.__init__(b, cfg)
    b._account = "U7654321"
    assert b.paper is True


def test_resolve_account_validates_managed_list() -> None:
    b = _StubBroker(account="DU1234567")
    b._managed_accounts = ["DU1234567", "DU9999999"]
    # Default routing.
    assert b._resolve_account(None) == "DU1234567"
    # Non-default but managed account is OK.
    assert b._resolve_account("DU9999999") == "DU9999999"
    # Unknown account raises.
    import pytest as _pytest
    with _pytest.raises(ValueError, match="not in managed accounts"):
        b._resolve_account("DU0000000")


def test_advisor_master_classifier() -> None:
    """DF and FU prefixes mark advisor master accounts that manage
    sub-accounts but can't take orders directly."""
    assert IBBroker._is_advisor_master("DFN394316") is True
    assert IBBroker._is_advisor_master("FU1234567") is True
    # DU and U are direct, tradeable accounts.
    assert IBBroker._is_advisor_master("DU1234567") is False
    assert IBBroker._is_advisor_master("U7654321") is False


def test_submit_market_order(broker: IBBroker) -> None:
    order = broker.submit_order(symbol="aapl", qty=10, side="buy")
    assert isinstance(order, OrderRecord)
    assert order.symbol == "AAPL"
    assert order.side == "buy"
    assert order.qty == 10.0
    assert order.status == "accepted"


def test_submit_limit_requires_price(broker: IBBroker) -> None:
    with pytest.raises(ValueError, match="limit_price required"):
        broker.submit_order(symbol="AAPL", qty=1, side="buy", order_type="limit")


def test_submit_rejects_unknown_type(broker: IBBroker) -> None:
    with pytest.raises(ValueError, match="Unsupported order_type"):
        broker.submit_order(symbol="AAPL", qty=1, side="buy", order_type="oco")


def test_close_position_full(broker: IBBroker) -> None:
    order = broker.close_position("AAPL")
    assert order.symbol == "AAPL"


def test_close_position_partial_pct(broker: IBBroker) -> None:
    order = broker.close_position("AAPL", percentage=50)
    assert order.symbol == "AAPL"


def test_close_position_validates_pct(broker: IBBroker) -> None:
    with pytest.raises(ValueError, match="percentage must be in"):
        broker.close_position("AAPL", percentage=150)


def test_close_position_qty_xor_pct(broker: IBBroker) -> None:
    with pytest.raises(ValueError, match="qty OR percentage"):
        broker.close_position("AAPL", qty=1, percentage=50)


def test_close_all_positions(broker: IBBroker) -> None:
    result = broker.close_all_positions()
    assert result["submitted"] == 1
    assert result["ok"] == 1
    assert result["failed"] == 0


def test_get_orders(broker: IBBroker) -> None:
    orders = broker.get_orders(limit=5)
    assert len(orders) == 2
    statuses = {o.status for o in orders}
    assert statuses == {"filled", "canceled"}


def test_get_orders_status_filter(broker: IBBroker) -> None:
    only_filled = broker.get_orders(limit=5, status="filled")
    assert len(only_filled) == 1
    assert only_filled[0].status == "filled"


def test_get_broker_returns_none_when_disabled() -> None:
    with patch.dict(os.environ, {"IB_BROKER_DISABLED": "1"}, clear=False):
        assert get_broker() is None


def test_get_broker_returns_broker_by_default() -> None:
    """get_broker() builds the wrapper eagerly; connection is lazy."""
    with patch.dict(os.environ, {}, clear=True):
        b = get_broker()
        assert b is not None
        assert isinstance(b, IBBroker)
        # Don't actually call any method — that would try to connect.


def test_trade_to_record_handles_none_fields() -> None:
    """The helper must not crash on a sparsely-populated trade object."""

    class _Status:
        status = "Submitted"
        filled = 0
        avgFillPrice = 0  # noqa: N815 — IB attribute name

    class _Order:
        permId = 12345  # noqa: N815
        action = "BUY"
        totalQuantity = 5  # noqa: N815
        orderType = "MKT"  # noqa: N815
        tif = "DAY"
        lmtPrice = 0  # noqa: N815

    class _Contract:
        symbol = "AAPL"

    class _Trade:
        contract = _Contract()
        order = _Order()
        orderStatus = _Status()  # noqa: N815
        log = []

    record = _trade_to_record(_Trade())
    assert record.symbol == "AAPL"
    assert record.side == "buy"
    assert record.qty == 5.0
    assert record.status == "accepted"  # IB "Submitted" -> "accepted"
    assert record.limit_price is None
