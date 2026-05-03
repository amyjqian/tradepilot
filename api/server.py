"""FastAPI server exposing scan + backtest + config endpoints.

CORS is open for the Vite dev server at http://localhost:5173. `/scan` results
are cached in-process for 60 seconds so dashboard polling is cheap. Providers
themselves are wrapped with a parquet-backed `CachedProvider` so OHLCV bars
persist across restarts — subsequent scans re-fetch only the delta from the
wrapped provider, saving IB pacing budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from scanner.backtest import backtest
from scanner.broker import IBBroker, Journal, KillSwitchActive, get_broker
from scanner.config import ScannerConfig
from scanner.data import MarketDataProvider, get_provider
from scanner.data.ib_provider import PacingBudgetExhausted
from scanner.engine import scan
from scanner.sector_rotation import (
    SECTOR_ETFS,
    SPY_BENCHMARK,
    get_active_constituents,
    run_sector_rotation,
)

log = logging.getLogger(__name__)

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
logging.getLogger("scanner.data.polygon_quotes").setLevel(logging.INFO)

app = FastAPI(title="TradePilot API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class _State:
    """Mutable server state — live config, scan cache, and provider pool.

    Providers are cached per-name so a single IB (or yfinance) client is
    reused across requests instead of being re-created — and, crucially,
    never re-connected — on every /scan call.
    """

    def __init__(self) -> None:
        self.config: ScannerConfig = ScannerConfig()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        self.cache_ttl_sec: float = 60.0
        self._providers: dict[str, MarketDataProvider] = {}
        self._providers_lock = threading.Lock()
        self._broker: IBBroker | None | _Sentinel = _UNSET
        self._broker_lock = threading.Lock()
        self._quote_streamer: Any = None
        self._quote_streamer_lock = threading.Lock()

    def broker(self) -> IBBroker | None:
        """Return the configured broker, or None if intentionally disabled.

        Cached after the first attempt. The connection itself is lazy
        inside IBBroker; this just builds the wrapper so we don't re-read
        env vars on every request.
        """
        with self._broker_lock:
            if self._broker is _UNSET:
                self._broker = get_broker()
            return self._broker  # type: ignore[return-value]

    def reset_broker(self) -> None:
        with self._broker_lock:
            self._broker = _UNSET

    def quote_streamer(self) -> Any:
        """Lazy singleton for the Polygon WebSocket quote streamer. The
        background loop is started on first use and shared across SSE
        clients so we never open more than one upstream connection.
        Raises RuntimeError if POLYGON_API_KEY is not configured.
        """
        with self._quote_streamer_lock:
            if self._quote_streamer is None:
                from scanner.data.polygon_quotes import PolygonQuoteStreamer
                s = PolygonQuoteStreamer.from_env()
                s.start()
                self._quote_streamer = s
            return self._quote_streamer

    def cache_get(self, key: str) -> dict[str, Any] | None:
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is None:
                return None
            ts, payload = hit
            if time.time() - ts > self.cache_ttl_sec:
                self._cache.pop(key, None)
                return None
            return payload

    def cache_put(self, key: str, payload: dict[str, Any]) -> None:
        with self._cache_lock:
            self._cache[key] = (time.time(), payload)

    def provider(self, name: str) -> MarketDataProvider:
        with self._providers_lock:
            existing = self._providers.get(name)
            if existing is not None:
                return existing
            # Wrap every provider in the parquet cache so fetched bars
            # persist across restarts and subsequent scans only pay for
            # the delta (or nothing, if the cache is still fresh).
            created = get_provider(name, cache_path=_CACHE_DIR)
            self._providers[name] = created
            return created

    def close_providers(self) -> None:
        with self._providers_lock:
            providers = list(self._providers.items())
            self._providers.clear()
        for name, p in providers:
            disconnect = getattr(p, "disconnect", None)
            if disconnect is None:
                continue
            try:
                disconnect()
            except Exception:
                log.exception("Provider %s disconnect failed", name)
        # Disconnect the broker too so its background loop thread exits.
        with self._broker_lock:
            broker = self._broker if not isinstance(self._broker, _Sentinel) else None
            self._broker = _UNSET
        if broker is not None:
            disc = getattr(broker, "disconnect", None)
            if callable(disc):
                try:
                    disc()
                except Exception:
                    log.exception("Broker disconnect failed")
        # Stop the Polygon WebSocket loop too.
        with self._quote_streamer_lock:
            streamer = self._quote_streamer
            self._quote_streamer = None
        if streamer is not None:
            try:
                streamer.stop()
            except Exception:
                log.exception("Quote streamer stop failed")


class _Sentinel:
    """Distinguishes 'broker not yet probed' from 'probed and unavailable'."""


_UNSET = _Sentinel()


_state = _State()


# Parquet cache lives here by default. Override with BULLISH_CACHE_DIR.
_CACHE_DIR = Path(
    os.environ.get(
        "BULLISH_CACHE_DIR",
        str(Path(__file__).resolve().parent.parent / "data_cache"),
    )
)


@app.on_event("shutdown")
def _on_shutdown() -> None:
    _state.close_providers()


class ScanRequest(BaseModel):
    provider: str = Field(default="polygon")
    tickers: list[str] | None = None
    interval: str | None = None
    lookback_days: int | None = None


class BacktestRequest(BaseModel):
    provider: str = Field(default="polygon")
    tickers: list[str] | None = None
    interval: str | None = None
    lookback_days: int | None = None
    holding_bars: int = 3
    target_pct: float = 2.0
    min_history: int = 30


class SectorRotationRequest(BaseModel):
    provider: str = Field(default="polygon")
    interval: str | None = None
    lookback_days: int | None = None
    top_n: int = Field(default=2, ge=1, le=5)


class WatchlistRequest(BaseModel):
    tickers: list[str]


class OrderRequest(BaseModel):
    symbol: str
    qty: float
    side: str  # "buy" / "sell"
    type: str = "market"  # "market" / "limit" / "stop" / "pegprim" / "midprice"
    time_in_force: str = "day"
    limit_price: float | None = None
    # Trigger price for plain stop orders (type == "stop"). Stops trigger
    # at this level and transmit as a market order.
    stop_price: float | None = None
    client_order_id: str | None = None
    # Captured at submit time so the journal can compute R-multiple and
    # tag the trade with the entry score. Both optional — orders placed
    # outside the dashboard (e.g. via TWS directly) won't carry these.
    planned_stop: float | None = None
    score_at_entry: float | None = None
    # Pegged-to-Primary (REL) parameters. Required when type == "pegprim".
    # `peg_offset` is the auxPrice tick offset above bid (BUY) or below
    # ask (SELL). `cap_price` is the hard limit price (ceiling for BUY,
    # floor for SELL).
    peg_offset: float | None = None
    cap_price: float | None = None
    # Optional IB account to route the order to. Must be in the broker's
    # managed-accounts list. None → broker's default account.
    account: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/config")
def get_config() -> dict[str, Any]:
    return _state.config.model_dump(mode="json")


@app.post("/config")
def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        new_cfg = ScannerConfig.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc
    _state.config = new_cfg
    return _state.config.model_dump(mode="json")


@app.get("/universe")
def get_universe() -> dict[str, list[str]]:
    return {"tickers": list(_state.config.default_tickers)}


_PRESETS_DIR = Path(__file__).resolve().parent.parent / "configs"


@app.get("/presets")
def list_presets() -> dict[str, list[dict[str, Any]]]:
    """Enumerate config JSON files in configs/ so the dashboard can offer them."""
    if not _PRESETS_DIR.exists():
        return {"presets": []}
    presets: list[dict[str, Any]] = []
    for fp in sorted(_PRESETS_DIR.glob("*.json")):
        try:
            cfg = ScannerConfig.from_json(fp)
        except Exception as exc:
            log.warning("Preset %s failed to load: %s", fp.name, exc)
            continue
        presets.append(
            {
                "name": fp.stem,
                "interval": cfg.interval,
                "lookback_days": cfg.lookback_days,
                "n_tickers": len(cfg.default_tickers),
            }
        )
    return {"presets": presets}


@app.post("/config/preset/{name}")
def apply_preset(name: str) -> dict[str, Any]:
    """Load configs/<name>.json and make it the active config."""
    # Defend against path traversal — names must be simple identifiers.
    if not name.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail=f"Invalid preset name: {name!r}")
    fp = _PRESETS_DIR / f"{name}.json"
    if not fp.exists():
        raise HTTPException(status_code=404, detail=f"Preset not found: {name}")
    try:
        _state.config = ScannerConfig.from_json(fp)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Preset load failed: {exc}") from exc
    return _state.config.model_dump(mode="json")


def _resolve_cfg(overrides: ScanRequest | BacktestRequest) -> ScannerConfig:
    cfg = _state.config.model_copy(deep=True)
    if overrides.interval is not None:
        cfg.interval = overrides.interval  # type: ignore[assignment]
    if overrides.lookback_days is not None:
        cfg.lookback_days = overrides.lookback_days
    return cfg


def _config_fingerprint(cfg: ScannerConfig) -> str:
    """Short, stable hash of the scoring-relevant config fields. Used in
    the /scan cache key so threshold/weight changes invalidate cached
    results — without this a `POST /config` followed by `/scan` returns
    a result computed under the previous config for up to the cache TTL.
    """
    payload = cfg.model_dump(
        mode="json",
        # `default_tickers` is already in the cache key as the resolved
        # symbol list; `interval` and `lookback_days` are too. Including
        # them here would just bloat the hash without changing keys.
        exclude={"default_tickers", "interval", "lookback_days"},
    )
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:10]


@app.post("/scan")
def post_scan(req: ScanRequest) -> dict[str, Any]:
    cfg = _resolve_cfg(req)
    tickers = [t.upper() for t in req.tickers] if req.tickers else list(cfg.default_tickers)
    cfg_fp = _config_fingerprint(cfg)
    cache_key = (
        f"{req.provider}|{cfg.interval}|{cfg.lookback_days}|"
        f"{','.join(sorted(tickers))}|cfg={cfg_fp}"
    )

    cached = _state.cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        provider = _state.provider(req.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        data = provider.get_bars_batch(tickers, cfg.interval, cfg.lookback_days)
    except PacingBudgetExhausted as exc:
        retry = max(1, int(round(exc.retry_after_sec)))
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(retry)},
        ) from exc
    except Exception as exc:
        log.exception("Data fetch failed")
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {exc!r}") from exc

    results = scan(data, cfg)
    payload = {
        "ran_at": datetime.now(UTC).isoformat(),
        "provider": req.provider,
        "interval": cfg.interval,
        "lookback_days": cfg.lookback_days,
        "n_candidates_scanned": len(data),
        "n_results": len(results),
        "results": [r.to_dict() for r in results],
    }
    _state.cache_put(cache_key, payload)
    return payload


@app.post("/backtest")
def post_backtest(req: BacktestRequest) -> dict[str, Any]:
    cfg = _resolve_cfg(req)
    tickers = [t.upper() for t in req.tickers] if req.tickers else list(cfg.default_tickers)

    try:
        provider = _state.provider(req.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        data = provider.get_bars_batch(tickers, cfg.interval, cfg.lookback_days)
    except PacingBudgetExhausted as exc:
        retry = max(1, int(round(exc.retry_after_sec)))
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(retry)},
        ) from exc
    except Exception as exc:
        log.exception("Data fetch failed")
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {exc!r}") from exc

    report = backtest(
        data, cfg,
        holding_bars=req.holding_bars,
        target_pct=req.target_pct,
        min_history=req.min_history,
    )
    payload = report.to_dict()
    payload.update(
        {
            "ran_at": datetime.now(UTC).isoformat(),
            "provider": req.provider,
            "interval": cfg.interval,
            "lookback_days": cfg.lookback_days,
            "holding_bars": req.holding_bars,
            "target_pct": req.target_pct,
        }
    )
    return payload


@app.post("/scan/sector-rotation")
def post_sector_rotation(req: SectorRotationRequest) -> dict[str, Any]:
    """Two-stage scan: rank sector ETFs, then run scanner on top sector's constituents."""
    cfg = _resolve_cfg(req)  # type: ignore[arg-type]

    try:
        provider = _state.provider(req.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sector_tickers = list(SECTOR_ETFS.keys()) + [SPY_BENCHMARK]
    # Use the live SSGA-backed map when available; falls back to the
    # hardcoded snapshot. Without this, tickers added to a sector since
    # the hardcoded list was curated would be silently dropped from the
    # scan because we never fetched their bars.
    active_map, _source = get_active_constituents()
    constituent_universe = sorted({t for tickers in active_map.values() for t in tickers})

    try:
        sector_data = provider.get_bars_batch(sector_tickers, cfg.interval, cfg.lookback_days)
        spy_df = sector_data.pop(SPY_BENCHMARK, None)
        constituent_data = provider.get_bars_batch(
            constituent_universe, cfg.interval, cfg.lookback_days
        )
    except PacingBudgetExhausted as exc:
        retry = max(1, int(round(exc.retry_after_sec)))
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(retry)},
        ) from exc
    except Exception as exc:
        log.exception("Sector-rotation data fetch failed")
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {exc!r}") from exc

    report = run_sector_rotation(
        sector_data, spy_df, constituent_data, cfg, top_n=req.top_n,
    )
    payload = report.to_dict()
    payload.update(
        {
            "ran_at": datetime.now(UTC).isoformat(),
            "provider": req.provider,
            "interval": cfg.interval,
            "lookback_days": cfg.lookback_days,
        }
    )
    return payload


_WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "configs" / "watchlist.json"


def _read_watchlist() -> list[str]:
    if not _WATCHLIST_PATH.exists():
        return list(_state.config.default_tickers)
    try:
        import json

        with _WATCHLIST_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        tickers = payload.get("tickers", [])
        return [str(t).upper() for t in tickers if t]
    except Exception as exc:
        log.warning("Failed to read watchlist: %s", exc)
        return list(_state.config.default_tickers)


def _write_watchlist(tickers: list[str]) -> list[str]:
    import json

    cleaned = [t.strip().upper() for t in tickers if t and t.strip()]
    deduped: list[str] = []
    seen: set[str] = set()
    for t in cleaned:
        if t not in seen:
            deduped.append(t)
            seen.add(t)
    _WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _WATCHLIST_PATH.open("w", encoding="utf-8") as fh:
        json.dump({"tickers": deduped}, fh, indent=2)
    return deduped


@app.get("/watchlist")
def get_watchlist() -> dict[str, list[str]]:
    return {"tickers": _read_watchlist()}


@app.put("/watchlist")
def put_watchlist(req: WatchlistRequest) -> dict[str, list[str]]:
    return {"tickers": _write_watchlist(req.tickers)}


@app.get("/broker/status")
def broker_status() -> dict[str, Any]:
    """Lightweight probe — does the API have IBKR wired up?

    The broker is wired up by default; we only return `connected: false`
    when `IB_BROKER_DISABLED=1` is set. Connection failures (TWS not
    running, wrong port, etc.) surface as 502 on /broker/account when
    the user actually tries to use it — there's no point pre-connecting
    just to render a status badge.

    `accounts` is the full list of managed accounts visible from this
    connection (populated lazily on first `/broker/account` call); the
    dashboard uses it to show an account dropdown when more than one
    account is reachable.
    """
    b = _state.broker()
    if b is None:
        return {
            "connected": False,
            "paper": None,
            "accounts": [],
            "default_account": None,
            "hint": (
                "IB broker is disabled. Unset IB_BROKER_DISABLED to enable, "
                "or set IB_BROKER_HOST / IB_BROKER_PORT / IB_BROKER_CLIENT_ID."
            ),
        }
    return {
        "connected": True,
        "paper": b.paper,
        "account": b.account,                 # legacy alias for default_account
        "default_account": b.default_account,
        "accounts": b.accounts,
        "hint": None,
    }


@app.get("/broker/account")
def broker_account() -> dict[str, Any]:
    b = _state.broker()
    if b is None:
        raise HTTPException(status_code=503, detail="Broker not configured")
    try:
        return b.get_account().to_dict()
    except Exception as exc:
        log.exception("IB get_account failed")
        raise HTTPException(status_code=502, detail=f"IB: {exc!r}") from exc


@app.get("/broker/positions")
def broker_positions() -> dict[str, Any]:
    b = _state.broker()
    if b is None:
        raise HTTPException(status_code=503, detail="Broker not configured")
    try:
        positions = b.get_positions()
    except Exception as exc:
        log.exception("IB get_positions failed")
        raise HTTPException(status_code=502, detail=f"IB: {exc!r}") from exc
    return {"positions": [p.to_dict() for p in positions]}


@app.post("/broker/close-all")
def broker_close_all() -> dict[str, Any]:
    """Kill switch — submit market closes for every open position."""
    b = _state.broker()
    if b is None:
        raise HTTPException(status_code=503, detail="Broker not configured")
    try:
        result = b.close_all_positions(cancel_orders=True)
    except Exception as exc:
        log.exception("IB close_all_positions failed")
        raise HTTPException(status_code=502, detail=f"IB: {exc!r}") from exc
    log.warning(
        "Kill switch fired — submitted=%d ok=%d failed=%d",
        result["submitted"], result["ok"], result["failed"],
    )
    return result


@app.post("/broker/orders")
def broker_submit_order(req: OrderRequest) -> dict[str, Any]:
    """Place a new order. Logs every attempt at INFO."""
    b = _state.broker()
    if b is None:
        raise HTTPException(status_code=503, detail="Broker not configured")

    side = req.side.lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    if req.qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be > 0")
    symbol = req.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    log.info(
        "Submitting order: paper=%s account=%s %s %s qty=%s type=%s tif=%s "
        "limit=%s stop=%s planned_stop=%s score=%s peg_offset=%s cap_price=%s",
        b.paper, req.account or b.default_account, side, symbol,
        req.qty, req.type, req.time_in_force,
        req.limit_price, req.stop_price,
        req.planned_stop, req.score_at_entry,
        req.peg_offset, req.cap_price,
    )
    try:
        order = b.submit_order(
            symbol=symbol,
            qty=req.qty,
            side=side,
            order_type=req.type,
            time_in_force=req.time_in_force,
            limit_price=req.limit_price,
            stop_price=req.stop_price,
            client_order_id=req.client_order_id,
            planned_stop=req.planned_stop,
            score_at_entry=req.score_at_entry,
            peg_offset=req.peg_offset,
            cap_price=req.cap_price,
            account=req.account,
        )
    except KillSwitchActive as exc:
        raise HTTPException(
            status_code=423,
            detail=str(exc),
            headers={"X-Risk-Locked": "daily_drawdown"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("IB submit_order failed")
        raise HTTPException(status_code=502, detail=f"IB: {exc!r}") from exc
    return order.to_dict()


@app.delete("/broker/positions/{symbol}")
def broker_close_position(
    symbol: str,
    percentage: float | None = None,
    qty: float | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Close a single position fully or partially.

    Pass `?percentage=50` for a 50% close, `?qty=5` for a 5-share close,
    or neither for a full close. `?account=DUxxxxxx` to target a
    non-default account.
    """
    b = _state.broker()
    if b is None:
        raise HTTPException(status_code=503, detail="Broker not configured")

    sym = symbol.strip().upper()
    log.info(
        "Close position: paper=%s account=%s symbol=%s qty=%s percentage=%s",
        b.paper, account or b.default_account, sym, qty, percentage,
    )
    try:
        order = b.close_position(sym, qty=qty, percentage=percentage, account=account)
    except KillSwitchActive as exc:
        raise HTTPException(
            status_code=423,
            detail=str(exc),
            headers={"X-Risk-Locked": "daily_drawdown"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("IB close_position failed")
        raise HTTPException(status_code=502, detail=f"IB: {exc!r}") from exc
    return order.to_dict()


@app.delete("/broker/orders")
def broker_cancel_orders(symbol: str) -> dict[str, Any]:
    """Cancel every still-working order for one symbol. Used by the
    dashboard's per-ticker "Cancel" button. Pass `?symbol=AAPL`."""
    b = _state.broker()
    if b is None:
        raise HTTPException(status_code=503, detail="Broker not configured")
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol is required")
    log.info("Cancel orders for symbol: %s", sym)
    try:
        return b.cancel_orders_for_symbol(sym)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("IB cancel_orders_for_symbol failed")
        raise HTTPException(status_code=502, detail=f"IB: {exc!r}") from exc


@app.get("/broker/orders")
def broker_get_orders(limit: int = 30, status: str = "all") -> dict[str, Any]:
    b = _state.broker()
    if b is None:
        raise HTTPException(status_code=503, detail="Broker not configured")
    capped_limit = max(1, min(limit, 200))
    try:
        orders = b.get_orders(limit=capped_limit, status=status)
    except Exception as exc:
        log.exception("IB get_orders failed")
        raise HTTPException(status_code=502, detail=f"IB: {exc!r}") from exc
    return {"orders": [o.to_dict() for o in orders]}


# ----------------------------------------------------------------------
# Real-time stream + risk/journal endpoints
# ----------------------------------------------------------------------

# Server-side journal singleton — the broker writes here from its event
# handlers; the read endpoints below open their own short-lived
# connections so the journal stays usable even when the broker isn't.
_JOURNAL_PATH = _CACHE_DIR / "journal.sqlite"
_journal_lock = threading.Lock()
_journal_singleton: Journal | None = None


def _get_journal() -> Journal:
    global _journal_singleton
    with _journal_lock:
        if _journal_singleton is None:
            _journal_singleton = Journal(_JOURNAL_PATH)
        return _journal_singleton


_SSE_HEARTBEAT_SEC = 15.0


@app.get("/broker/stream")
async def broker_stream() -> StreamingResponse:
    """Server-sent events: pushes account / position / order / fill /
    risk deltas as ib_async events fire. The frontend subscribes on
    mount and stops polling. Initial `event: snapshot` carries the
    current cache so reconnecting clients don't need a separate REST
    round-trip.
    """
    b = _state.broker()
    if b is None:
        raise HTTPException(status_code=503, detail="Broker not configured")

    # Force a connect so reqAccountUpdates is wired and events flow.
    # `get_account` is idempotent — subsequent calls are cheap.
    try:
        await asyncio.to_thread(b.get_account)
    except Exception as exc:
        log.warning("Broker connect failed before SSE stream: %r", exc)

    loop = asyncio.get_running_loop()
    queue, sub_id = b.state.subscribe(loop)
    snapshot = b.state.snapshot()

    async def event_gen() -> AsyncIterator[str]:
        try:
            yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_HEARTBEAT_SEC
                    )
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            b.state.unsubscribe(sub_id)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/broker/risk-status")
def broker_risk_status() -> dict[str, Any]:
    b = _state.broker()
    if b is None:
        raise HTTPException(status_code=503, detail="Broker not configured")
    return b.state.risk_status()


@app.post("/broker/risk-reset")
def broker_risk_reset() -> dict[str, Any]:
    """Clear the daily-drawdown kill flag. Manual only — the trader has
    to consciously re-arm before new orders are accepted.
    """
    b = _state.broker()
    if b is None:
        raise HTTPException(status_code=503, detail="Broker not configured")
    b.state.reset_kill()
    log.warning("Risk kill switch manually reset")
    return b.state.risk_status()


@app.get("/broker/journal/trades")
def broker_journal_trades(limit: int = 100) -> dict[str, Any]:
    journal = _get_journal()
    trades = journal.list_trades(limit=limit)
    return {"trades": trades}


@app.get("/broker/journal/stats")
def broker_journal_stats() -> dict[str, Any]:
    journal = _get_journal()
    return journal.stats()


# ----------------------------------------------------------------------
# Real-time quotes — Polygon WebSocket → SSE multiplexer
# ----------------------------------------------------------------------


def _polygon_event_to_payload(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a Polygon WS event into the chart-shaped payload the
    dashboard's RealtimeChart expects.

    `AM` (minute aggregate) → `bar` event (drives the candlestick).
    `T`  (trade)            → `trade` event (drives the live price line
                              and the toolbar pill — sub-second cadence).

    Returns None for events we don't handle so the caller can skip the
    SSE frame entirely.
    """
    kind = str(ev.get("ev") or "")
    if kind == "AM":
        # Polygon timestamps are millisecond epoch; lightweight-charts
        # wants epoch seconds for intraday data.
        start_ms = int(ev.get("s") or 0)
        return {
            "kind": "bar",
            "payload": {
                "time": start_ms // 1000,
                "open": ev.get("o"),
                "high": ev.get("h"),
                "low": ev.get("l"),
                "close": ev.get("c"),
                "volume": ev.get("v"),
                "vwap": ev.get("vw"),
            },
        }
    if kind == "T":
        ts_ms = int(ev.get("t") or 0)
        price = ev.get("p")
        if price is None:
            return None
        return {
            "kind": "trade",
            "payload": {
                "price": float(price),
                "size": float(ev.get("s") or 0),
                "ts": ts_ms // 1000,
            },
        }
    return None


_QUOTES_HEARTBEAT_SEC = 15.0
# Server-side throttle for `T` (trade) events. Polygon emits
# 50-500 prints/sec on liquid names — way more than a chart needs and
# more than an SSE pipe wants to relay. We drop intermediate trades
# and forward at most ~10/sec per SSE client; the most recent print
# always lands within ~100 ms of arrival, which is well under any
# perceptible latency.
_TRADE_THROTTLE_SEC = 0.1


@app.get("/quotes/stream/{symbol}")
async def quotes_stream(symbol: str) -> StreamingResponse:
    """Server-sent events: real-time minute aggregates for a symbol.

    The first SSE client for a symbol opens the upstream Polygon
    subscription; later clients share it. When the last client drops,
    we send `unsubscribe` to Polygon.

    Free Polygon Stocks plans don't include WebSocket — auth will fail
    in the streamer's reconnect loop and no bars will arrive. The
    endpoint still returns 200 + heartbeats so the client can show
    "no data flowing" rather than getting a 5xx.
    """
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    try:
        streamer = _state.quote_streamer()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
    last_trade_ts = [0.0]  # mutable cell for the throttle gate

    def on_event(ev: dict[str, Any]) -> None:
        # Polygon WS callbacks fire on the streamer's loop thread; hand
        # off to the FastAPI loop the queue belongs to.
        if ev.get("ev") == "T":
            now = time.monotonic()
            # Drop trades that arrive within the throttle window. The
            # next trade after the cooldown gets through — so the chart
            # sees a representative current price every ~100 ms during
            # RTH, not every print (which can be 100+/sec).
            if now - last_trade_ts[0] < _TRADE_THROTTLE_SEC:
                return
            last_trade_ts[0] = now
        try:
            loop.call_soon_threadsafe(_safe_put, queue, ev)
        except RuntimeError:
            pass

    # Subscribe to both 1-minute aggregates and trade prints. AM drives
    # the candle/volume update; T drives the live-price ticker and the
    # ghost line. Plans without T entitlement see Polygon reject the T
    # sub but accept AM, so the chart still works (just minute-cadence).
    sub_id = streamer.subscribe(sym, channels=("AM", "T"), callback=on_event)

    async def event_gen() -> AsyncIterator[str]:
        try:
            yield f"event: connected\ndata: {json.dumps({'symbol': sym})}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(
                        queue.get(), timeout=_QUOTES_HEARTBEAT_SEC
                    )
                    payload = _polygon_event_to_payload(ev)
                    if payload is None:
                        continue
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            try:
                streamer.unsubscribe(sub_id)
            except Exception:
                log.debug("quote unsubscribe raised", exc_info=True)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _safe_put(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    """Non-blocking enqueue with drop-oldest backpressure. Mirrors the
    pattern in scanner.broker.state — slow consumers don't block the
    upstream WS thread; we drop the oldest event instead.
    """
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
            queue.put_nowait(event)
        except Exception:
            pass


@app.get("/quotes/history/{symbol}")
def quotes_history(
    symbol: str,
    interval: str = "1m",
    lookback_days: int = 1,
) -> dict[str, Any]:
    """Initial chart bars from Polygon REST. The dashboard's
    RealtimeChart calls this once on mount and then layers on the SSE
    stream for updates. We always go through Polygon here — TradingView
    rendered any provider before, but live updates only come from
    Polygon, so the history must match.
    """
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    capped_lookback = max(1, min(lookback_days, 365))
    try:
        provider = _state.provider("polygon")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        df = provider.get_bars(sym, interval, capped_lookback)
    except PacingBudgetExhausted as exc:
        retry = max(1, int(round(exc.retry_after_sec)))
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(retry)},
        ) from exc
    except Exception as exc:
        log.warning("quotes_history failed for %s: %s", sym, exc)
        raise HTTPException(
            status_code=502, detail=f"history fetch failed: {exc!r}"
        ) from exc

    bars: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        bars.append(
            {
                "time": int(idx.timestamp()),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        )
    return {
        "symbol": sym,
        "interval": interval,
        "lookback_days": capped_lookback,
        "bars": bars,
    }
