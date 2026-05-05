"""Tests for the Polygon (massive.com) data provider.

We don't hit the network — the HTTP client is replaced with httpx's
MockTransport so we can feed canned `Aggregates v2` responses and assert
the normalizer + rate-limiter + retry behavior end-to-end.
"""

from __future__ import annotations

import time

import httpx
import pandas as pd
import pytest

from scanner.data.polygon_provider import (
    PolygonAuthError,
    PolygonConfig,
    PolygonProvider,
    PolygonRateLimited,
    _MinuteLimiter,
    _normalize_aggregates,
)


def _aggs_payload(bars: list[dict]) -> dict:
    return {
        "ticker": "AAPL",
        "queryCount": len(bars),
        "resultsCount": len(bars),
        "adjusted": True,
        "results": bars,
        "status": "OK",
    }


def _make_provider(handler) -> PolygonProvider:
    cfg = PolygonConfig(api_key="test-key", rate_limit_per_min=0)
    p = PolygonProvider(config=cfg)
    p._client = httpx.Client(  # type: ignore[attr-defined]
        base_url=cfg.base_url,
        transport=httpx.MockTransport(handler),
        params={"apiKey": cfg.api_key},
    )
    return p


def test_normalize_aggregates_renames_and_sorts() -> None:
    payload = _aggs_payload([
        {"o": 102, "h": 103, "l": 101, "c": 102.5, "v": 200, "t": 1700000060000},
        {"o": 100, "h": 101, "l": 99, "c": 100.5, "v": 100, "t": 1700000000000},
    ])
    df = _normalize_aggregates(payload, "AAPL")
    assert df is not None
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    # Sorted ascending by timestamp regardless of input order.
    assert df.index.is_monotonic_increasing
    assert df.index.tz is not None
    assert df["open"].iloc[0] == 100
    assert df["open"].iloc[1] == 102


def test_normalize_aggregates_empty() -> None:
    assert _normalize_aggregates({"results": []}, "AAPL") is None
    assert _normalize_aggregates({}, "AAPL") is None


def test_get_bars_parses_response() -> None:
    bars = [
        {"o": 100, "h": 101, "l": 99, "c": 100.5, "v": 100, "t": 1700000000000},
        {"o": 101, "h": 102, "l": 100, "c": 101.5, "v": 150, "t": 1700086400000},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        # Sanity check the URL shape — the docstring promises
        # /v2/aggs/ticker/{T}/range/{m}/{tf}/{from}/{to}.
        assert "/v2/aggs/ticker/AAPL/range/1/day/" in req.url.path
        return httpx.Response(200, json=_aggs_payload(bars))

    p = _make_provider(handler)
    df = p.get_bars("AAPL", "1d", lookback_days=30)
    assert len(df) == 2
    assert df["close"].iloc[-1] == 101.5


def test_auth_error_raises_immediately() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": "ERROR"})

    p = _make_provider(handler)
    with pytest.raises(PolygonAuthError):
        p.get_bars("AAPL", "1d", lookback_days=30)


def test_rate_limit_retries_then_succeeds() -> None:
    state = {"calls": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=_aggs_payload([
            {"o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "t": 1700000000000},
        ]))

    p = _make_provider(handler)
    df = p.get_bars("AAPL", "1d", lookback_days=30)
    assert state["calls"] == 2
    assert len(df) == 1


def test_rate_limit_exhausts_retries() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    cfg = PolygonConfig(api_key="k", rate_limit_per_min=0, max_retries=1)
    p = PolygonProvider(config=cfg)
    p._client = httpx.Client(  # type: ignore[attr-defined]
        base_url=cfg.base_url,
        transport=httpx.MockTransport(handler),
        params={"apiKey": cfg.api_key},
    )
    with pytest.raises(PolygonRateLimited):
        p.get_bars("AAPL", "1d", lookback_days=30)


def test_get_bars_batch_skips_failed_tickers() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "BADTICK" in req.url.path:
            return httpx.Response(200, json={"status": "OK", "results": []})
        return httpx.Response(200, json=_aggs_payload([
            {"o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "t": 1700000000000},
        ]))

    p = _make_provider(handler)
    out = p.get_bars_batch(["AAPL", "BADTICK", "MSFT"], "1d", lookback_days=30)
    assert set(out.keys()) == {"AAPL", "MSFT"}


def test_unsupported_interval() -> None:
    p = _make_provider(lambda _r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="Unsupported interval"):
        p.get_bars("AAPL", "7m", lookback_days=10)


def test_minute_limiter_blocks_when_full() -> None:
    """The limiter must enforce its window — the (max_per_window+1)th call
    should not return until the window has rolled over."""
    lim = _MinuteLimiter(max_per_window=2, window_sec=0.2)
    lim.acquire()
    lim.acquire()
    start = time.monotonic()
    lim.acquire()
    elapsed = time.monotonic() - start
    # The 3rd call must wait for one of the first two stamps to age out.
    assert elapsed >= 0.15


def test_polygon_config_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POLYGON_API_KEY"):
        PolygonConfig.from_env()


def test_polygon_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYGON_API_KEY", "abc")
    monkeypatch.setenv("POLYGON_API_BASE", "https://example.com")
    monkeypatch.setenv("POLYGON_RATE_LIMIT_PER_MIN", "300")
    cfg = PolygonConfig.from_env()
    assert cfg.api_key == "abc"
    assert cfg.base_url == "https://example.com"
    assert cfg.rate_limit_per_min == 300


def test_factory_resolves_polygon_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both 'polygon' and 'massive' should resolve to PolygonProvider."""
    monkeypatch.setenv("POLYGON_API_KEY", "abc")
    from scanner.data import get_provider

    p1 = get_provider("polygon")
    p2 = get_provider("massive")
    assert isinstance(p1, PolygonProvider)
    assert isinstance(p2, PolygonProvider)
    p1.disconnect()
    p2.disconnect()


def test_factory_rejects_unknown() -> None:
    from scanner.data import get_provider

    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("doesnt-exist")


def test_normalize_aggregates_dedupes_and_drops_nan() -> None:
    payload = _aggs_payload([
        {"o": 100, "h": 101, "l": 99, "c": 100.5, "v": 100, "t": 1700000000000},
        # duplicate timestamp — should be deduped (keep last)
        {"o": 100.1, "h": 101.1, "l": 99.1, "c": 100.6, "v": 110, "t": 1700000000000},
        {"o": 102, "h": None, "l": 101, "c": 102.5, "v": 200, "t": 1700086400000},
    ])
    df = _normalize_aggregates(payload, "AAPL")
    assert df is not None
    assert df.index.is_unique
    # NaN row dropped.
    assert len(df) == 1
    assert df["close"].iloc[0] == pytest.approx(100.6)
