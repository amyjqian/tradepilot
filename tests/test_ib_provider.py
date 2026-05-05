"""Tests for the IB provider — rate limiter and config.

Connection / live-request tests are skipped unless `IB_TEST_LIVE=1` is set,
since they require a running TWS / IB Gateway with market data permissions.
"""

from __future__ import annotations

import os
import time

import pytest

from scanner.data.ib_provider import (
    _INTERVAL_TO_BAR_SIZE,
    _MAX_DURATION_DAYS,
    IBConfig,
    IBProvider,
    PacingBudgetExhausted,
    _SlidingWindowLimiter,
)


def test_sliding_window_limiter_allows_up_to_cap() -> None:
    lim = _SlidingWindowLimiter(max_requests=5, window_sec=60.0)
    t0 = time.time()
    for _ in range(5):
        lim.acquire()
    assert time.time() - t0 < 0.1  # five free slots, no sleep


def test_sliding_window_limiter_blocks_on_overflow() -> None:
    lim = _SlidingWindowLimiter(max_requests=3, window_sec=0.5)
    for _ in range(3):
        lim.acquire()
    start = time.time()
    lim.acquire()  # should block ~0.5 s waiting for oldest to expire
    elapsed = time.time() - start
    assert 0.3 < elapsed < 1.5, f"unexpected wait: {elapsed:.2f}s"


def test_sliding_window_limiter_recycles_slots() -> None:
    lim = _SlidingWindowLimiter(max_requests=2, window_sec=0.2)
    lim.acquire()
    lim.acquire()
    time.sleep(0.25)  # both old slots should now be outside the window
    start = time.time()
    lim.acquire()
    lim.acquire()
    assert time.time() - start < 0.1


def test_sliding_window_limiter_rejects_bad_params() -> None:
    with pytest.raises(ValueError):
        _SlidingWindowLimiter(max_requests=0, window_sec=10.0)
    with pytest.raises(ValueError):
        _SlidingWindowLimiter(max_requests=5, window_sec=0.0)


def test_acquire_fails_fast_when_wait_exceeds_cap() -> None:
    lim = _SlidingWindowLimiter(max_requests=2, window_sec=10.0)
    lim.acquire()
    lim.acquire()
    t0 = time.time()
    with pytest.raises(PacingBudgetExhausted) as excinfo:
        lim.acquire(max_wait_sec=0.5)
    elapsed = time.time() - t0
    assert elapsed < 0.2, f"should fail fast, not sleep; took {elapsed:.2f}s"
    assert excinfo.value.retry_after_sec > 0.5


def test_reserve_takes_n_slots_atomically() -> None:
    lim = _SlidingWindowLimiter(max_requests=5, window_sec=60.0)
    lim.reserve(3)
    assert lim.snapshot()["in_window"] == 3
    lim.reserve(2)
    assert lim.snapshot()["in_window"] == 5


def test_reserve_fails_fast_when_batch_exceeds_remaining() -> None:
    lim = _SlidingWindowLimiter(max_requests=5, window_sec=10.0)
    lim.reserve(4)
    t0 = time.time()
    with pytest.raises(PacingBudgetExhausted):
        lim.reserve(3, max_wait_sec=0.5)
    assert time.time() - t0 < 0.2
    # No slots should have been taken by the failed call.
    assert lim.snapshot()["in_window"] == 4


def test_reserve_rejects_batch_bigger_than_cap() -> None:
    lim = _SlidingWindowLimiter(max_requests=5, window_sec=10.0)
    with pytest.raises(PacingBudgetExhausted):
        lim.reserve(10)


def test_ib_config_defaults() -> None:
    cfg = IBConfig()
    assert cfg.port == 8401
    assert cfg.max_requests_per_window <= 60  # respects IB ceiling
    assert cfg.window_sec == 600.0
    assert cfg.min_request_spacing_sec > 0


def test_ib_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IB_HOST", "10.0.0.42")
    monkeypatch.setenv("IB_PORT", "7497")
    monkeypatch.setenv("IB_CLIENT_ID", "99")
    cfg = IBConfig.from_env()
    assert cfg.host == "10.0.0.42"
    assert cfg.port == 7497
    assert cfg.client_id == 99


def test_interval_mapping_covers_scanner_intervals() -> None:
    # Scanner supports these intervals; IB must map every one.
    for interval in ("1m", "5m", "15m", "1h", "1d"):
        assert interval in _INTERVAL_TO_BAR_SIZE
        assert interval in _MAX_DURATION_DAYS


def test_ib_provider_rejects_unknown_interval() -> None:
    provider = IBProvider()
    with pytest.raises(ValueError, match="Unsupported interval"):
        provider.get_bars("AAPL", "7m", 10)


def test_ib_provider_pacing_status_snapshot() -> None:
    provider = IBProvider()
    snap = provider.pacing_status()
    assert snap["capacity"] == provider.config.max_requests_per_window
    assert snap["in_window"] == 0
    assert snap["oldest_age"] is None


@pytest.mark.skipif(
    os.environ.get("IB_TEST_LIVE") != "1",
    reason="Live IB test skipped (set IB_TEST_LIVE=1 with TWS running to enable)",
)
def test_ib_provider_live_fetch() -> None:
    """Smoke test against a running TWS/Gateway. Fetches one ticker."""
    provider = IBProvider()
    try:
        df = provider.get_bars("AAPL", "1d", 10)
    finally:
        provider.disconnect()
    assert len(df) > 3
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None
