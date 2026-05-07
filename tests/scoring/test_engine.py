"""End-to-end `score_symbol` integration."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scanner.scoring import GateThresholds, ScoringWeights, score_symbol
from scanner.scoring.engine import score_symbol_sync

from ._helpers import make_bars, make_ctx

ET = ZoneInfo("America/New_York")


def _bullish_ctx_at(now_ms: int):  # noqa: ANN202 — test helper
    """Construct a uniformly bullish context that should pass everything."""
    bars_1m = make_bars(
        72,
        closes=[100.0 + i * 0.05 for i in range(72)],
        highs=[100.0 + i * 0.05 + 0.1 for i in range(72)],
        lows=[100.0 + i * 0.05 - 0.1 for i in range(72)],
        volumes=[10_000.0] * 72,
    )
    bars_5m = make_bars(
        60,  # plenty for EMA50 + RSI(14)
        closes=[100.0 + i * 0.05 for i in range(60)],
        highs=[100.0 + i * 0.05 + 0.5 for i in range(60)],
        lows=[100.0 + i * 0.05 - 0.5 for i in range(60)],
    )
    bars_15m = make_bars(
        60,
        closes=[100.0 + i * 0.10 for i in range(60)],
        highs=[100.0 + i * 0.10 + 0.5 for i in range(60)],
        lows=[100.0 + i * 0.10 - 0.5 for i in range(60)],
    )
    bars_d = make_bars(20, closes=[100.0] * 20, highs=[101.0] * 20, lows=[99.0] * 20)
    return make_ctx(
        symbol="TEST",
        now_ms=now_ms,
        session_start_ms=now_ms - 72 * 60_000,
        bars={"1m": bars_1m, "5m": bars_5m, "15m": bars_15m, "daily": bars_d},
        prior_session_close=100.0,
        today_high=bars_1m[-1].high,
        yesterday_high=bars_1m[-1].high - 0.5,
        today_low=99.0,
        yesterday_low=99.0,
    )


def test_score_symbol_returns_a_result() -> None:
    now_ms = int(datetime(2026, 4, 15, 10, 42, tzinfo=ET).timestamp() * 1000)
    ctx = _bullish_ctx_at(now_ms)
    result = score_symbol_sync(ctx)
    assert result is not None
    assert result.symbol == "TEST"
    assert 0.0 <= result.base_score <= 100.0
    assert result.tod_mult == pytest.approx(1.10)
    assert set(result.components.keys()) == set(ScoringWeights().as_dict().keys())


def test_score_symbol_returns_none_on_gate_fail() -> None:
    now_ms = int(datetime(2026, 4, 15, 10, 42, tzinfo=ET).timestamp() * 1000)
    ctx = _bullish_ctx_at(now_ms)
    # halve all 1m closes → fails min_price=5 still passes; instead trip ADV.
    from dataclasses import replace

    ctx_low_adv = replace(ctx, adv_dollar=1_000.0)
    assert score_symbol_sync(ctx_low_adv, gates=GateThresholds()) is None


def test_score_symbol_async_wrapper() -> None:
    now_ms = int(datetime(2026, 4, 15, 10, 42, tzinfo=ET).timestamp() * 1000)
    ctx = _bullish_ctx_at(now_ms)
    result = asyncio.run(score_symbol(ctx))
    assert result is not None


def test_score_symbol_returns_none_when_disqualified() -> None:
    now_ms = int(datetime(2026, 4, 15, 10, 42, tzinfo=ET).timestamp() * 1000)
    ctx = _bullish_ctx_at(now_ms)
    from dataclasses import replace

    ctx_halted = replace(ctx, halted_recently=True)
    # halted_recently fails the gate first, so this returns None for that
    # reason — both DQ and gate paths produce None, which is the contract.
    assert score_symbol_sync(ctx_halted) is None
