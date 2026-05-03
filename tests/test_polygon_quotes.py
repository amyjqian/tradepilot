"""Unit tests for the Polygon quote streamer's subscriber bookkeeping.

We don't open a real WebSocket — those tests are just exercising the
dispatch / refcount / reconnect-state logic that lives outside the
network path. The main loop is covered by integration testing.
"""

from __future__ import annotations

from scanner.data.polygon_quotes import PolygonQuoteStreamer


def _make_streamer() -> PolygonQuoteStreamer:
    # API key value doesn't matter — we never call _main(); we only
    # exercise the registry methods.
    return PolygonQuoteStreamer(api_key="test")


def test_subscribe_records_pair_and_marks_pending() -> None:
    s = _make_streamer()
    seen: list[dict] = []
    sid = s.subscribe("AAPL", channels=("AM",), callback=seen.append)
    assert "AM.AAPL" in s._sent_subs
    # Dispatch an event matching that pair — the callback should fire.
    s._dispatch({"ev": "AM", "sym": "AAPL", "c": 100})
    assert len(seen) == 1
    assert seen[0]["c"] == 100
    s.unsubscribe(sid)


def test_multiple_subscribers_same_symbol_only_one_upstream() -> None:
    s = _make_streamer()
    a: list[dict] = []
    b: list[dict] = []
    sid_a = s.subscribe("AAPL", channels=("AM",), callback=a.append)
    sid_b = s.subscribe("AAPL", channels=("AM",), callback=b.append)
    # Even with two subscribers, only one upstream pair was registered.
    assert s._sent_subs == {"AM.AAPL"}
    s._dispatch({"ev": "AM", "sym": "AAPL"})
    assert len(a) == 1
    assert len(b) == 1
    # Dropping one subscriber doesn't unsubscribe upstream.
    s.unsubscribe(sid_a)
    assert "AM.AAPL" in s._sent_subs
    # Dropping the second one does.
    s.unsubscribe(sid_b)
    assert "AM.AAPL" not in s._sent_subs


def test_dispatch_routes_by_channel() -> None:
    s = _make_streamer()
    am: list[dict] = []
    t: list[dict] = []
    s.subscribe("AAPL", channels=("AM",), callback=am.append)
    s.subscribe("AAPL", channels=("T",), callback=t.append)
    s._dispatch({"ev": "AM", "sym": "AAPL"})
    s._dispatch({"ev": "T", "sym": "AAPL"})
    assert len(am) == 1
    assert len(t) == 1
    # No cross-talk.
    s._dispatch({"ev": "AM", "sym": "AAPL"})
    assert len(t) == 1


def test_dispatch_ignores_unknown_symbol() -> None:
    s = _make_streamer()
    seen: list[dict] = []
    s.subscribe("AAPL", channels=("AM",), callback=seen.append)
    s._dispatch({"ev": "AM", "sym": "MSFT"})
    assert seen == []


def test_callback_exception_does_not_kill_other_subscribers() -> None:
    s = _make_streamer()
    bad_calls: list[int] = []
    good: list[dict] = []

    def bad(_ev: dict) -> None:
        bad_calls.append(1)
        raise RuntimeError("boom")

    s.subscribe("AAPL", channels=("AM",), callback=bad)
    s.subscribe("AAPL", channels=("AM",), callback=good.append)
    s._dispatch({"ev": "AM", "sym": "AAPL"})
    assert bad_calls == [1]
    assert len(good) == 1


def test_subscribe_multiple_channels_atomic() -> None:
    s = _make_streamer()
    events: list[dict] = []
    sid = s.subscribe("AAPL", channels=("AM", "T"), callback=events.append)
    assert s._sent_subs == {"AM.AAPL", "T.AAPL"}
    s.unsubscribe(sid)
    assert s._sent_subs == set()


def test_unsubscribe_unknown_id_is_no_op() -> None:
    s = _make_streamer()
    s.subscribe("AAPL", channels=("AM",), callback=lambda _: None)
    s.unsubscribe(99999)  # not a real id
    assert "AM.AAPL" in s._sent_subs


def test_from_env_requires_key(monkeypatch) -> None:
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="POLYGON_API_KEY"):
        PolygonQuoteStreamer.from_env()


def test_from_env_reads_cluster_override(monkeypatch) -> None:
    monkeypatch.setenv("POLYGON_API_KEY", "abc")
    monkeypatch.setenv("POLYGON_WS_CLUSTER", "wss://example.com/stocks")
    s = PolygonQuoteStreamer.from_env()
    assert s._cluster == "wss://example.com/stocks"
    assert s._api_key == "abc"
