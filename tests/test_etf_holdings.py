"""Tests for the SSGA sector-holdings fetcher + JSON cache.

The HTTP fetch is exercised in two ways:
  - via `httpx.MockTransport` against a synthesized XLSX so we know
    the parser is correct without going to the network.
  - via the cache load/save round-trip with a real temp dir.
"""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import openpyxl
import pytest

from scanner.data.etf_holdings import (
    _normalize_ticker,
    fetch_ssga_holdings,
    load_constituents,
    refresh_all_constituents,
    save_constituents,
)


def _build_ssga_xlsx(rows: list[tuple[str, str]]) -> bytes:
    """Synthesize a workbook in the same shape SSGA publishes: 4 metadata
    rows (Fund Name / Ticker Symbol / Holdings: As of ... / blank), the
    column-header row, then the rows passed in (name, ticker)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "holdings"
    ws.append(["Fund Name:", "Test Sector SPDR ETF"])
    ws.append(["Ticker Symbol:", "TEST"])
    ws.append(["Holdings:", "As of 01-May-2026"])
    ws.append([])
    ws.append([
        "Name", "Ticker", "Identifier", "SEDOL", "Weight",
        "Sector", "Shares Held", "Local Currency",
    ])
    for name, ticker in rows:
        ws.append([name, ticker, "", "", 1.0, "-", 100, "USD"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _stub_client(xlsx_bytes: bytes) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda _req: httpx.Response(
            200,
            content=xlsx_bytes,
            headers={
                "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
        )
    )


def test_normalize_ticker() -> None:
    assert _normalize_ticker("brk.b") == "BRK-B"
    assert _normalize_ticker("AAPL") == "AAPL"
    assert _normalize_ticker(" CASH_USD ") is None
    assert _normalize_ticker("") is None
    assert _normalize_ticker(None) is None
    assert _normalize_ticker("-") is None


def test_fetch_ssga_holdings_parses_real_format(monkeypatch: pytest.MonkeyPatch) -> None:
    xlsx = _build_ssga_xlsx([
        ("APPLE INC", "AAPL"),
        ("MICROSOFT CORP", "MSFT"),
        ("BERKSHIRE HATHAWAY INC CL B", "BRK.B"),
        # Cash-line row should be skipped.
        ("CASH USD", "CASH_USD"),
        # Duplicate (e.g. dual-class listing) should be skipped on second seen.
        ("APPLE INC", "AAPL"),
    ])

    def patched_get(url: str, **kwargs: object) -> httpx.Response:
        client = httpx.Client(transport=_stub_client(xlsx))
        return client.get(url)

    monkeypatch.setattr("scanner.data.etf_holdings.httpx.get", patched_get)
    out = fetch_ssga_holdings("xlk")
    assert out == ["AAPL", "MSFT", "BRK-B"]


def test_fetch_ssga_holdings_raises_on_missing_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """If SSGA radically changes the format and 'Ticker' header isn't
    found, raise rather than returning a silently-empty list."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["This is", "not the", "format you", "expected"])
    ws.append(["just", "a", "few", "rows"])
    buf = io.BytesIO()
    wb.save(buf)
    xlsx = buf.getvalue()

    def patched_get(url: str, **kwargs: object) -> httpx.Response:
        client = httpx.Client(transport=_stub_client(xlsx))
        return client.get(url)

    monkeypatch.setattr("scanner.data.etf_holdings.httpx.get", patched_get)
    with pytest.raises(ValueError, match="No 'Ticker' column"):
        fetch_ssga_holdings("xlk")


def test_fetch_ssga_holdings_propagates_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(503))

    def patched_get(url: str, **kwargs: object) -> httpx.Response:
        client = httpx.Client(transport=transport)
        return client.get(url)

    monkeypatch.setattr("scanner.data.etf_holdings.httpx.get", patched_get)
    with pytest.raises(httpx.HTTPStatusError):
        fetch_ssga_holdings("xlk")


def test_refresh_skips_failed_etfs(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _build_ssga_xlsx([("APPLE INC", "AAPL")])

    def patched_get(url: str, **kwargs: object) -> httpx.Response:
        # Pretend XLE is broken upstream, others succeed.
        if "xle" in url:
            client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
        else:
            client = httpx.Client(transport=_stub_client(good))
        return client.get(url)

    monkeypatch.setattr("scanner.data.etf_holdings.httpx.get", patched_get)
    out = refresh_all_constituents(["XLK", "XLE", "XLF"])
    assert set(out.keys()) == {"XLK", "XLF"}
    assert out["XLK"] == ["AAPL"]
    assert "XLE" not in out


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "sector_holdings.json"
    save_constituents(path, {"XLK": ["AAPL", "MSFT"], "XLF": ["JPM"]})
    loaded, fetched_at = load_constituents(path)
    assert loaded == {"XLK": ["AAPL", "MSFT"], "XLF": ["JPM"]}
    assert fetched_at is not None  # was set by save_constituents


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    loaded, fetched_at = load_constituents(tmp_path / "nope.json")
    assert loaded == {}
    assert fetched_at is None


def test_load_corrupt_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json {{{")
    loaded, fetched_at = load_constituents(path)
    assert loaded == {}
    assert fetched_at is None


def test_get_active_constituents_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BULLISH_CACHE_DIR", str(tmp_path))
    from scanner.sector_rotation import (
        SECTOR_CONSTITUENTS,
        get_active_constituents,
    )
    cmap, source = get_active_constituents()
    assert source == "hardcoded"
    assert cmap == SECTOR_CONSTITUENTS


def test_get_active_constituents_prefers_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BULLISH_CACHE_DIR", str(tmp_path))
    save_constituents(
        tmp_path / "sector_holdings.json",
        {"XLK": ["NEW1", "NEW2"]},
    )
    from scanner.sector_rotation import get_active_constituents

    cmap, source = get_active_constituents()
    assert source.startswith("live")
    # Live data overrides hardcoded for XLK.
    assert cmap["XLK"] == ["NEW1", "NEW2"]
    # Other sectors still fall back to hardcoded.
    assert "XLF" in cmap
    assert len(cmap["XLF"]) > 0
