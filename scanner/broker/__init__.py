"""Broker integration.

Currently only Interactive Brokers (TWS / IB Gateway) via ib_async. The
broker is used for: account state, open positions, recent orders, the
kill-switch flat-close, and order placement.

The data provider (`scanner.data.ib_provider`) and this broker hold
distinct ib_async connections — different client IDs, different env vars
— because the data side runs `readonly=True` and the broker can't.
"""

from __future__ import annotations

from scanner.broker.ib_broker import (
    AccountSnapshot,
    IBBroker,
    IBBrokerConfig,
    IBNotConfigured,
    KillSwitchActive,
    OrderRecord,
    Position,
    get_broker,
)
from scanner.broker.journal import Journal
from scanner.broker.state import BrokerState

# Type alias kept for the FastAPI handlers that still type-hint with the
# old name. The class is the same.
Broker = IBBroker

__all__ = [
    "AccountSnapshot",
    "Broker",
    "BrokerState",
    "IBBroker",
    "IBBrokerConfig",
    "IBNotConfigured",
    "Journal",
    "KillSwitchActive",
    "OrderRecord",
    "Position",
    "get_broker",
]
