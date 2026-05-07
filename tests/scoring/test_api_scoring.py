"""End-to-end smoke tests for the /scoring API endpoints.

These exercise the FastAPI app against the synthetic provider so they
don't need network. They cover both `POST /scoring/scan` (one-shot) and
`GET /scoring/stream` (SSE) — the latter with `live=false` so we don't
require Polygon WS plumbing.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from api.server import _state, app


@pytest.fixture(autouse=True)
def _reset_runners():
    """Reset the scoring-runner cache between tests so stale state doesn't
    bleed across them."""
    _state._scoring_runners.clear()
    _state._scoring_feeds.clear()
    yield
    _state._scoring_runners.clear()
    _state._scoring_feeds.clear()


def test_post_scoring_scan_synthetic_returns_payload() -> None:
    async def go() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.post(
                "/scoring/scan",
                json={"provider": "synthetic", "tickers": ["AAPL"], "top_n": 5},
            )
            r.raise_for_status()
            return r.json()

    payload = asyncio.run(go())
    assert payload["n_candidates_scanned"] >= 1
    assert "rankings" in payload
    if payload["rankings"]:
        first = payload["rankings"][0]
        assert "components" in first
        assert "final_score" in first
        assert set(first["components"].keys()) >= {
            "rvol_30m",
            "rvol_cumulative",
            "momentum_atr",
            "vwap_distance_atr",
            "trend_stack_5m",
            "mtf_alignment",
            "rsi_intraday",
            "breakout_proximity",
            "clean_structure",
        }


def test_scoring_stream_route_is_registered() -> None:
    """Smoke check that `/scoring/stream` is on the FastAPI app.

    We don't drive the endpoint with a client because httpx's
    `ASGITransport` buffers streaming responses until the handler exits —
    cancelling the request doesn't propagate to the generator, so the
    test would hang on the heartbeat loop. The streaming behavior itself
    is covered by the runner-level test in `test_runner.py`
    (`test_subscribe_and_run_cycle_emits_events`), and the event payload
    shape by `test_post_scoring_scan_synthetic_returns_payload` above.
    """
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/scoring/stream" in paths
    assert "/scoring/scan" in paths
