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
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
from pathlib import Path
from collections.abc import Callable
from typing import Any, AsyncIterator

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from scanner.backtest import backtest
from scanner.broker import IBBroker, IBBrokerConfig, Journal, KillSwitchActive, get_broker
from scanner.broker.connections import (
    ConnectionConfig,
    load_aliases,
    load_connections,
    save_aliases,
    save_connections,
)
from scanner.config import ScannerConfig
from scanner.data import MarketDataProvider, get_provider
from scanner.data.ib_provider import PacingBudgetExhausted
from scanner.diagnostics import warnings as _warning_bus
from scanner.engine import scan
from scanner.scoring import (
    DisqualifierConfig,
    GateThresholds,
    ScoringWeights,
    TierThresholds,
)
from scanner.scoring.builders import build_states
from scanner.scoring.runner import ScannerRunner
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
logging.getLogger("scanner.scoring").setLevel(logging.INFO)
logging.getLogger("scanner.scoring.builders").setLevel(logging.INFO)


def _broker_disabled() -> bool:
    """Read at request time so the user can flip it without restart by
    rewriting .env (dev) or `kill -HUP` later. Set
    `TRADEPILOT_BROKER_DISABLED=1` to skip IBKR auto-connect, hide
    broker UI in the dashboard, and short-circuit broker endpoints."""
    val = os.environ.get("TRADEPILOT_BROKER_DISABLED", "").strip().lower()
    return val in {"1", "true", "yes", "on"}


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
        # Multi-connection broker registry. Keys are connection labels
        # from connections.json. Brokers are instantiated lazily — first
        # call to broker(label) builds the IBBroker; ib_async's TCP
        # connect is in turn lazy inside the broker, so a configured
        # connection that the UI never touches costs nothing.
        self._broker_lock = threading.RLock()
        self._connections: dict[str, IBBroker] = {}
        self._connection_configs: list[ConnectionConfig] | None = None
        self._quote_streamer: Any = None
        self._quote_streamer_lock = threading.Lock()
        # Per-scanner scoring runners. Keyed by a tuple of (provider, sorted
        # ticker tuple) so different SSE clients with different watchlists
        # get independent runners. Lazily started.
        self._scoring_runners: dict[str, Any] = {}
        self._scoring_runners_lock = threading.Lock()
        self._scoring_feeds: dict[str, Any] = {}

    # -- multi-connection broker registry ----------------------------------

    def list_connections(self) -> list[ConnectionConfig]:
        """Connection definitions in display order. Loaded from disk on
        first call and cached; mutating endpoints invalidate via
        `_replace_connections`."""
        with self._broker_lock:
            if self._connection_configs is None:
                self._connection_configs = load_connections()
            return list(self._connection_configs)

    def _replace_connections(self, items: list[ConnectionConfig]) -> None:
        """Persist a new connection list and invalidate the in-memory
        cache. Live brokers for now-removed labels are left running so
        in-flight requests don't crash; they'll be cleaned up on shutdown
        or on explicit disconnect."""
        save_connections(items)
        with self._broker_lock:
            self._connection_configs = list(items)

    def _resolve_label(self, label: str | None) -> str | None:
        """Pick the default connection label when the caller didn't.

        Order of preference: label explicitly passed → first
        `auto_connect` config → first config overall → None (no
        connections defined at all).
        """
        configs = self.list_connections()
        if not configs:
            return None
        if label is not None:
            return label if any(c.label == label for c in configs) else None
        auto = next((c for c in configs if c.auto_connect), None)
        return (auto or configs[0]).label

    def broker(self, label: str | None = None) -> IBBroker | None:
        """Return the broker for the given connection (default if None).

        Lazily instantiates the IBBroker the first time a label is
        touched. The TCP connect inside IBBroker is also lazy, so
        instantiation here is essentially free.
        """
        resolved = self._resolve_label(label)
        if resolved is None:
            return None
        with self._broker_lock:
            existing = self._connections.get(resolved)
            if existing is not None:
                return existing
            cfg = next(
                (c for c in self.list_connections() if c.label == resolved),
                None,
            )
            if cfg is None:
                return None
            broker_cfg = cfg.to_broker_config(_CACHE_DIR / "journal.sqlite")
            broker = IBBroker(broker_cfg)
            self._connections[resolved] = broker
            return broker

    def all_brokers(self) -> list[tuple[str, IBBroker]]:
        """(label, broker) for every connection with an instantiated
        broker. Does not trigger lazy instantiation — `eager_init` does
        that. Used by aggregating endpoints."""
        with self._broker_lock:
            return list(self._connections.items())

    def eager_init_auto_connect(self) -> None:
        """Instantiate all `auto_connect` brokers up-front so aggregating
        reads (positions, orders) see them without a prior per-label
        request. Connections still establish lazily on first IB call.
        """
        for c in self.list_connections():
            if c.auto_connect:
                self.broker(c.label)

    def account_to_label(self, account: str) -> str | None:
        """Reverse-lookup: which connection owns this IB account?
        Returns None if no instantiated broker reports it."""
        if not account:
            return None
        for label, broker in self.all_brokers():
            try:
                if account in broker.accounts:
                    return label
            except Exception:
                continue
        return None

    def disconnect_label(self, label: str) -> bool:
        """Disconnect and remove one broker by label. Returns True if a
        broker existed for that label."""
        with self._broker_lock:
            broker = self._connections.pop(label, None)
        if broker is None:
            return False
        disc = getattr(broker, "disconnect", None)
        if callable(disc):
            try:
                disc()
            except Exception:
                log.exception("Broker %s disconnect failed", label)
        return True

    def reset_broker(self) -> None:
        """Tear down every connection. Used by tests and by callers that
        previously assumed the single-broker design."""
        with self._broker_lock:
            brokers = list(self._connections.items())
            self._connections.clear()
        for label, broker in brokers:
            disc = getattr(broker, "disconnect", None)
            if callable(disc):
                try:
                    disc()
                except Exception:
                    log.exception("Broker %s disconnect failed", label)

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

    def scoring_runner(
        self,
        *,
        provider_name: str,
        tickers: list[str],
        attach_live_feed: bool,
        cadence_seconds: float = 60.0,
        halt_lookback_seconds: float = 120.0,
    ) -> tuple[Any, str]:
        """Lazy singleton scoring runner for a (provider, tickers, cadence) tuple.

        Bootstraps state via `build_states` on first request. If
        `attach_live_feed` is True, also subscribes to the shared Polygon
        WebSocket so 1m bars and quotes flow into the runner state. The
        runner's asyncio cycle loop is started on the calling event loop.

        The cache key includes `cadence_seconds` so a Watchlist@5m and
        Watchlist@15m produce two distinct runners (otherwise the second
        client gets the first's cadence).

        `halt_lookback_seconds` widens the heuristic halt threshold for
        the live path. The spec's 30s default assumes real exchange halt
        data; we approximate from "time since last closed 1m bar." A
        Polygon AM event for minute N's close arrives a few seconds into
        minute N+1, so a 5m or 15m cycle that fires right around a minute
        boundary can see `now - last_bar ≈ 60s` and false-positive at
        30s. 120s is generous enough to absorb that lag while still
        flagging real halts (>2 min of no prints).
        """
        from scanner.scoring.builders import build_states
        from scanner.scoring.config import GateThresholds
        from scanner.scoring.feeds import PolygonRunnerFeed
        from scanner.scoring.runner import ScannerRunner

        cadence_tag = f"{int(cadence_seconds)}s"
        key = f"{provider_name}|{cadence_tag}|{','.join(sorted(tickers))}"
        with self._scoring_runners_lock:
            existing = self._scoring_runners.get(key)
            if existing is not None:
                return existing, key

            provider = self.provider(provider_name)
            states = build_states(provider, tickers)
            runner = ScannerRunner(
                scanner_id=key,
                refresh_interval_seconds=cadence_seconds,
                gates=GateThresholds(halt_lookback_seconds=halt_lookback_seconds),
                # Live cycles fire at clock-aligned cadence boundaries
                # plus a 5s lag so Polygon's AM event for the just-closed
                # bar has time to arrive before we score against it.
                align_to_boundary=True,
                boundary_lag_seconds=5.0,
            )
            for state in states.values():
                runner.add_symbol(state)

            if attach_live_feed:
                try:
                    streamer = self.quote_streamer()
                    feed = PolygonRunnerFeed(streamer, runner)
                    feed.start()
                    self._scoring_feeds[key] = feed
                except Exception:
                    log.exception("Scoring live feed could not be attached for %s", key)

            self._scoring_runners[key] = runner
            return runner, key

    def stop_scoring_runners(self) -> None:
        with self._scoring_runners_lock:
            runners = list(self._scoring_runners.items())
            feeds = list(self._scoring_feeds.items())
            self._scoring_runners.clear()
            self._scoring_feeds.clear()
        for _, feed in feeds:
            try:
                feed.stop()
            except Exception:
                log.debug("Scoring feed stop failed", exc_info=True)
        for _, runner in runners:
            try:
                stop = getattr(runner, "stop", None)
                if stop is not None:
                    # ScannerRunner.stop is async; best-effort fire-and-
                    # forget on whichever loop is current.
                    asyncio.get_event_loop().run_until_complete(stop())
            except Exception:
                log.debug("Scoring runner stop failed", exc_info=True)

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
        # Disconnect every broker so background loop threads exit.
        with self._broker_lock:
            brokers = list(self._connections.items())
            self._connections.clear()
        for label, broker in brokers:
            disc = getattr(broker, "disconnect", None)
            if callable(disc):
                try:
                    disc()
                except Exception:
                    log.exception("Broker %s disconnect failed", label)
        # Stop the Polygon WebSocket loop too.
        with self._quote_streamer_lock:
            streamer = self._quote_streamer
            self._quote_streamer = None
        if streamer is not None:
            try:
                streamer.stop()
            except Exception:
                log.exception("Quote streamer stop failed")


_state = _State()


# Parquet cache lives here by default. Override with BULLISH_CACHE_DIR.
_CACHE_DIR = Path(
    os.environ.get(
        "BULLISH_CACHE_DIR",
        str(Path(__file__).resolve().parent.parent / "data_cache"),
    )
)


@app.on_event("startup")
def _on_startup() -> None:
    """Pre-instantiate auto-connect brokers so aggregating reads
    (positions across all connections) see every configured connection
    on the very first call. Each broker's TCP connect remains lazy.

    Skipped when `TRADEPILOT_BROKER_DISABLED` is set — the dashboard
    will hide all broker UI and the API's broker endpoints will return
    a disabled-state payload.
    """
    if _broker_disabled():
        log.info("Broker disabled via TRADEPILOT_BROKER_DISABLED — skipping eager init")
        return
    try:
        _state.eager_init_auto_connect()
    except Exception:
        log.exception("eager_init_auto_connect failed (non-fatal)")


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


class ScoringScanRequest(BaseModel):
    """Request for the per-minute scoring engine (`/scoring/scan`)."""

    provider: str = Field(default="polygon")
    tickers: list[str] | None = None
    top_n: int = Field(default=50, ge=1, le=500)
    # Hard ceiling so a misbehaving provider can't hang the dashboard.
    # Returns 504 when exceeded.
    timeout_sec: float = Field(default=45.0, ge=1.0, le=600.0)
    intraday_lookback_days: int = Field(default=5, ge=1, le=30)
    weights: dict[str, float] | None = None
    gates: dict[str, float] | None = None
    tiers: dict[str, float] | None = None


class ScoringScanMultiRequest(BaseModel):
    """Request for the multi-cadence scoring endpoint.

    For each cadence in `cadences`, the engine evaluates state at the
    most recent cadence boundary (anchored to today's 09:30 ET session
    start), truncating bars accordingly. This produces meaningfully
    different rankings per cadence in one-shot mode — without it, all
    cadences would see identical in-progress bars and score identically.

    `eval_at_ms` enables verification mode: pin the evaluation timestamp
    to a chosen past moment instead of `now`. The intraday lookback is
    auto-extended to cover the history gap, and the session anchor
    rebases to the calendar day containing `eval_at_ms`.
    """

    provider: str = Field(default="polygon")
    tickers: list[str] | None = None
    top_n: int = Field(default=50, ge=1, le=500)
    cadences: list[int] = Field(default_factory=lambda: [60, 120, 300, 900])
    timeout_sec: float = Field(default=60.0, ge=1.0, le=600.0)
    intraday_lookback_days: int = Field(default=5, ge=1, le=120)
    eval_at_ms: int | None = None
    weights: dict[str, float] | None = None
    gates: dict[str, float] | None = None
    tiers: dict[str, float] | None = None


class ScoringSectorScanRequest(BaseModel):
    """Request for the sector-driven per-minute scan (`/scoring/sector-scan`).

    Stage 1: rank `SECTOR_ETFS` by 5-bar relative strength + trend bonus.
    Stage 2: score the top `top_sectors` sectors' pooled constituents
    using the same cadence-boundary logic as `/scoring/scan-multi` —
    each cadence evaluates at its most recent boundary so 1m/2m/5m/15m
    panels show meaningfully different rankings.

    `eval_at_ms` enables verify mode (same semantics as scan-multi):
    evaluate at a chosen past timestamp instead of `now`. Sector ranking
    itself still uses daily bars ending at real-now (sector trends move
    on a daily timescale; not worth backfilling daily history to a
    verify date).
    """

    provider: str = Field(default="polygon")
    top_sectors: int = Field(default=2, ge=1, le=11)
    sector_lookback_days: int = Field(default=10, ge=5, le=60)
    top_n: int = Field(default=50, ge=1, le=500)
    cadences: list[int] = Field(default_factory=lambda: [60, 120, 300, 900])
    eval_at_ms: int | None = None
    timeout_sec: float = Field(default=60.0, ge=1.0, le=600.0)
    intraday_lookback_days: int = Field(default=5, ge=1, le=120)
    weights: dict[str, float] | None = None
    gates: dict[str, float] | None = None
    tiers: dict[str, float] | None = None


class OrderTarget(BaseModel):
    """One destination for a fan-out order: which connection to route
    through, and which IB account on that connection to credit. Either
    field may be omitted; in that case the server falls back to the
    default connection / the broker's default account."""

    connection: str | None = None
    account: str | None = None


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
    # Multi-target fan-out. Each entry produces one IB placeOrder. If
    # omitted, the server uses `connection` + `account` (legacy single-
    # target shape) or, lacking both, the default connection + default
    # account.
    targets: list[OrderTarget] | None = None
    # Legacy single-target shorthand. Equivalent to
    # `targets=[OrderTarget(connection=..., account=...)]`.
    connection: str | None = None
    account: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/warnings/recent")
def warnings_recent() -> dict[str, Any]:
    """Latest non-fatal warnings (Polygon 429s, IB pacing exhaustion,
    etc.). The dashboard subscribes to /warnings/stream for live
    updates; this is a fallback for clients that prefer polling."""
    return {"warnings": _warning_bus.recent()}


@app.get("/warnings/stream")
async def warnings_stream() -> StreamingResponse:
    """Server-sent events stream of non-fatal warnings. Sends an
    initial `event: snapshot` with the recent ring buffer so the UI
    can backfill any warnings the user missed before opening the tab,
    then pushes each new warning as a `data:` event."""
    loop = asyncio.get_running_loop()
    queue, sub_id = _warning_bus.subscribe(loop)
    snapshot = _warning_bus.recent()

    async def event_gen() -> AsyncIterator[str]:
        try:
            yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            _warning_bus.unsubscribe(sub_id)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


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

    # Liquidity is a daily property — on intraday scans the in-bar tail(20)
    # would measure 20 *minutes* of volume against a daily-tuned floor and
    # reject nearly every ticker. Fetch a small daily lookback so the engine
    # can apply the universe filter on daily bars regardless of scan interval.
    # CachedProvider keeps repeat fetches cheap (parquet-backed). If the
    # daily fetch fails we fall back to the in-bar window — the scan still
    # runs, just with the old approximate behavior.
    daily_data: dict[str, pd.DataFrame] | None = None
    if cfg.interval != "1d":
        try:
            daily_data = provider.get_bars_batch(tickers, "1d", 30)
        except PacingBudgetExhausted as exc:
            log.warning("Daily liquidity fetch hit pacing budget (%s); using in-bar fallback", exc)
        except Exception as exc:
            log.warning("Daily liquidity fetch failed (%s); using in-bar fallback", exc)

    results = scan(data, cfg, daily_data=daily_data)
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


class _ScoringTimeout(RuntimeError):
    """Raised when build_states exceeds the request's timeout_sec budget."""


def _run_with_timeout(fn: Callable[[], Any], *, timeout_sec: float) -> Any:
    """Run `fn` on a worker thread with a hard wall-clock cap.

    The worker thread keeps running after timeout (we can't kill native
    threads in Python) but the request returns 504 so the dashboard
    doesn't hang. Background workers eventually finish and exit.
    """
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn)
        try:
            return fut.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError as exc:
            raise _ScoringTimeout(
                f"scoring scan exceeded {timeout_sec:.0f}s timeout — "
                f"check Polygon connectivity / rate limits / watchlist size"
            ) from exc


def _score_result_to_dict(r: Any) -> dict[str, Any]:
    return {
        "symbol": r.symbol,
        "timestamp": r.timestamp,
        "base_score": r.base_score,
        "tod_mult": r.tod_mult,
        "final_score": r.final_score,
        "bias_15m": r.bias_15m,
        "tier": r.tier,
        "flags": list(r.flags),
        "components": {
            name: {
                "name": c.name,
                "strength": c.strength,
                "raw": c.raw,
            }
            for name, c in r.components.items()
        },
    }


@app.post("/scoring/scan")
def post_scoring_scan(req: ScoringScanRequest) -> dict[str, Any]:
    """One-shot run of the per-minute scoring engine.

    Pulls daily + 1m bars for each ticker via the existing provider, builds
    a `SymbolState` with seeded today's session, runs `score_symbol` for
    each, and returns rankings sorted by `final_score`. No caching — each
    call evaluates against the latest data.
    """
    tickers = (
        [t.upper() for t in req.tickers]
        if req.tickers
        else list(_state.config.default_tickers)
    )
    log.info(
        "/scoring/scan: provider=%s tickers=%d top_n=%d",
        req.provider, len(tickers), req.top_n,
    )
    t_start = time.perf_counter()
    try:
        provider = _state.provider(req.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        weights = ScoringWeights(**req.weights) if req.weights else ScoringWeights()
        # One-shot scans run on Polygon's REST data, which lags real-time
        # by 60–120s (REST only returns closed minute bars). The default
        # 30s halt heuristic is calibrated for the live WebSocket runner
        # and would flag every symbol as "halted_recently" on REST data.
        # Bump to 600s here unless the caller explicitly overrides.
        gate_overrides = dict(req.gates or {})
        gate_overrides.setdefault("halt_lookback_seconds", 600.0)
        gates = GateThresholds(**gate_overrides)
        tiers = TierThresholds(**req.tiers) if req.tiers else TierThresholds()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc

    try:
        states = _run_with_timeout(
            lambda: build_states(provider, tickers, intraday_lookback_days=req.intraday_lookback_days),
            timeout_sec=req.timeout_sec,
        )
    except _ScoringTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except PacingBudgetExhausted as exc:
        retry = max(1, int(round(exc.retry_after_sec)))
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": str(retry)}
        ) from exc
    except Exception as exc:
        log.exception("Scoring data fetch failed")
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {exc!r}") from exc

    runner = ScannerRunner(
        scanner_id="oneshot",
        weights=weights,
        gates=gates,
        tier_thresholds=tiers,
        dq_config=DisqualifierConfig(),
        top_n=req.top_n,
    )
    for state in states.values():
        runner.add_symbol(state)

    t_score = time.perf_counter()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    rankings, rejected = runner.score_all_diagnostic(now_ms)

    # Aggregate which gate / DQ filters fired most often. The dashboard
    # uses this to tell the user "21 symbols failed atr_pct_20d" rather
    # than just showing an empty results table.
    reason_counts: dict[str, int] = {}
    for rej in rejected:
        for reason in rej.reasons:
            tag = reason.split(" ", 1)[0]
            reason_counts[tag] = reason_counts.get(tag, 0) + 1

    log.info(
        "/scoring/scan: scored %d → %d ranked, %d rejected in %.2fs (total %.2fs)",
        len(states), len(rankings), len(rejected),
        time.perf_counter() - t_score, time.perf_counter() - t_start,
    )
    if reason_counts:
        log.info("/scoring/scan rejection reasons: %s",
                 sorted(reason_counts.items(), key=lambda x: -x[1]))

    return {
        "ran_at": datetime.now(UTC).isoformat(),
        "provider": req.provider,
        "n_candidates_scanned": len(states),
        "n_results": len(rankings),
        "n_rejected": len(rejected),
        "rejection_summary": [
            {"reason": k, "count": v}
            for k, v in sorted(reason_counts.items(), key=lambda x: -x[1])
        ],
        "rankings": [_score_result_to_dict(r) for r in rankings],
        "rejected": [
            {"symbol": rej.symbol, "reasons": list(rej.reasons)}
            for rej in rejected
        ],
    }


@app.post("/scoring/scan-multi")
def post_scoring_scan_multi(req: ScoringScanMultiRequest) -> dict[str, Any]:
    """Run the engine at each cadence's most-recent boundary.

    Single backend fetch, multiple evaluation timestamps. The dashboard's
    4-panel cadence view consumes this so each panel's ranking is
    genuinely cadence-aware (not just the same one-shot snapshot).
    """
    from scanner.scoring.builders import session_start_ms as session_start_ms_for
    from scanner.scoring.runner import most_recent_cadence_boundary

    log.info(
        "/scoring/scan-multi: provider=%s cadences=%s top_n=%d",
        req.provider, req.cadences, req.top_n,
    )
    t_start = time.perf_counter()
    tickers = (
        [t.upper() for t in req.tickers]
        if req.tickers
        else list(_state.config.default_tickers)
    )
    try:
        provider = _state.provider(req.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        weights = ScoringWeights(**req.weights) if req.weights else ScoringWeights()
        gate_overrides = dict(req.gates or {})
        gate_overrides.setdefault("halt_lookback_seconds", 600.0)
        gates = GateThresholds(**gate_overrides)
        tiers = TierThresholds(**req.tiers) if req.tiers else TierThresholds()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc

    # Verification mode: pin the evaluation moment. Today rebases to the
    # ET calendar day containing `eval_at_ms`, and the intraday lookback
    # auto-extends (capped at 30d) so the fetched 1m history reaches
    # that day. Users wanting verification farther back than 30 days can
    # set `intraday_lookback_days` explicitly above the cap.
    real_now_ms = int(datetime.now(UTC).timestamp() * 1000)
    if req.eval_at_ms is not None:
        actual_now_ms = int(req.eval_at_ms)
        eval_dt_et = datetime.fromtimestamp(actual_now_ms / 1000, tz=ET)
        today_date = eval_dt_et.date()
        gap_days = max(0, (datetime.now(ET).date() - today_date).days)
        auto_lookback = min(gap_days + 5, 30)
        intraday_lookback = max(req.intraday_lookback_days, auto_lookback)
        log.info(
            "/scoring/scan-multi: verify mode @ %s ET (gap=%dd, lookback=%dd)",
            eval_dt_et.isoformat(), gap_days, intraday_lookback,
        )
    else:
        actual_now_ms = real_now_ms
        today_date = datetime.now(ET).date()
        intraday_lookback = req.intraday_lookback_days

    try:
        full_states = _run_with_timeout(
            lambda: build_states(
                provider,
                tickers,
                today=today_date,
                intraday_lookback_days=intraday_lookback,
                # In verify mode, tell build_states the eval timestamp
                # so the cached provider can short-circuit Polygon delta
                # fetches when the on-disk cache already extends past
                # this moment. Repeated verify clicks at the same past
                # `eval_at_ms` become fully cache-served after the first.
                eval_at_ms=req.eval_at_ms,
            ),
            timeout_sec=req.timeout_sec,
        )
    except _ScoringTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except PacingBudgetExhausted as exc:
        retry = max(1, int(round(exc.retry_after_sec)))
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": str(retry)},
        ) from exc
    except Exception as exc:
        log.exception("Multi-cadence data fetch failed")
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {exc!r}") from exc

    today_session_ms = session_start_ms_for(today_date)

    panels: dict[str, Any] = {}
    for cadence_secs in req.cadences:
        eval_ms = most_recent_cadence_boundary(
            actual_now_ms, cadence_secs, today_session_ms
        )
        runner = ScannerRunner(
            scanner_id=f"multi-{cadence_secs}",
            weights=weights, gates=gates, tier_thresholds=tiers,
            dq_config=DisqualifierConfig(), top_n=req.top_n,
        )
        for state in full_states.values():
            runner.add_symbol(state.truncate_to(eval_ms))
        rankings, rejected = runner.score_all_diagnostic(eval_ms)
        reason_counts: dict[str, int] = {}
        for rej in rejected:
            for reason in rej.reasons:
                tag = reason.split(" ", 1)[0]
                reason_counts[tag] = reason_counts.get(tag, 0) + 1
        panels[str(cadence_secs)] = {
            "cadence_seconds": cadence_secs,
            "eval_ms": eval_ms,
            "n_candidates_scanned": len(full_states),
            "n_results": len(rankings),
            "n_rejected": len(rejected),
            "rankings": [_score_result_to_dict(r) for r in rankings],
            "rejected": [
                {"symbol": rej.symbol, "reasons": list(rej.reasons)}
                for rej in rejected
            ],
            "rejection_summary": [
                {"reason": k, "count": v}
                for k, v in sorted(reason_counts.items(), key=lambda x: -x[1])
            ],
        }

    log.info(
        "/scoring/scan-multi done: %d panels in %.2fs",
        len(panels), time.perf_counter() - t_start,
    )

    return {
        "ran_at": datetime.now(UTC).isoformat(),
        "provider": req.provider,
        "now_ms": actual_now_ms,
        "session_start_ms": today_session_ms,
        "verify_mode": req.eval_at_ms is not None,
        "panels": panels,
    }


@app.post("/scoring/sector-scan")
def post_scoring_sector_scan(req: ScoringSectorScanRequest) -> dict[str, Any]:
    """Two-stage per-minute scan: rank sectors, score top sectors' constituents.

    Stage 1 reuses the daily-bar sector rotation logic (`rank_sectors`)
    against the 11 SPDR sector ETFs, ranked by 5-bar relative strength
    vs SPY plus a trend bonus.

    Stage 2 takes the constituents of the top `top_sectors` sectors
    (deduped, preserving sector order) and runs the per-minute scoring
    engine on them — same gates / weights / DQ as `/scoring/scan`.
    """
    log.info(
        "/scoring/sector-scan: provider=%s top_sectors=%d top_n=%d",
        req.provider, req.top_sectors, req.top_n,
    )
    t_start = time.perf_counter()
    try:
        provider = _state.provider(req.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        weights = ScoringWeights(**req.weights) if req.weights else ScoringWeights()
        gate_overrides = dict(req.gates or {})
        gate_overrides.setdefault("halt_lookback_seconds", 600.0)
        gates = GateThresholds(**gate_overrides)
        tiers = TierThresholds(**req.tiers) if req.tiers else TierThresholds()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc

    # --- Stage 1: rank sectors on daily bars --------------------------------
    sector_tickers = list(SECTOR_ETFS.keys()) + [SPY_BENCHMARK]
    try:
        sector_data = _run_with_timeout(
            lambda: provider.get_bars_batch(
                sector_tickers, "1d", req.sector_lookback_days
            ),
            timeout_sec=req.timeout_sec,
        )
    except _ScoringTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except PacingBudgetExhausted as exc:
        retry = max(1, int(round(exc.retry_after_sec)))
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": str(retry)},
        ) from exc
    except Exception as exc:
        log.exception("Sector ETF fetch failed")
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {exc!r}") from exc
    spy_df = sector_data.pop(SPY_BENCHMARK, None)

    from scanner.sector_rotation import rank_sectors

    ranked = rank_sectors(sector_data, spy_df)
    if not ranked:
        raise HTTPException(status_code=502, detail="Sector ranking returned no rows")
    n = max(1, min(req.top_sectors, len(ranked)))
    tops = ranked[:n]

    # --- Stage 2: pool top sectors' constituents and score them -------------
    constituents_map, source = get_active_constituents()
    log.info("/scoring/sector-scan: constituents source=%s", source)
    # Dedup *only* for the unified scoring list (so cross-listed names
    # don't score twice). The per-sector display map uses the raw
    # holdings — matching the legacy `/scan/sector-rotation` behavior in
    # `run_sector_rotation()` so the dashboard's sector chips show each
    # ETF's full holding list.
    seen: set[str] = set()
    constituents: list[str] = []
    for top in tops:
        for ticker in constituents_map.get(top.etf, []):
            if ticker not in seen:
                seen.add(ticker)
                constituents.append(ticker)
    constituents_by_sector: dict[str, list[str]] = {
        top.etf: list(constituents_map.get(top.etf, [])) for top in tops
    }

    # --- Verify-mode rebasing (same semantics as /scoring/scan-multi) ----
    from scanner.scoring.builders import session_start_ms as session_start_ms_for
    from scanner.scoring.runner import most_recent_cadence_boundary

    if req.eval_at_ms is not None:
        actual_now_ms = int(req.eval_at_ms)
        eval_dt_et = datetime.fromtimestamp(actual_now_ms / 1000, tz=ET)
        today_date = eval_dt_et.date()
        gap_days = max(0, (datetime.now(ET).date() - today_date).days)
        auto_lookback = min(gap_days + 5, 30)
        intraday_lookback = max(req.intraday_lookback_days, auto_lookback)
        log.info(
            "/scoring/sector-scan: verify mode @ %s ET (gap=%dd, lookback=%dd)",
            eval_dt_et.isoformat(), gap_days, intraday_lookback,
        )
    else:
        actual_now_ms = int(datetime.now(UTC).timestamp() * 1000)
        today_date = datetime.now(ET).date()
        intraday_lookback = req.intraday_lookback_days
    today_session_ms = session_start_ms_for(today_date)

    sectors_payload = _sectors_payload(ranked, tops, constituents_by_sector)

    if not constituents:
        return {
            "ran_at": datetime.now(UTC).isoformat(),
            "provider": req.provider,
            "now_ms": actual_now_ms,
            "session_start_ms": today_session_ms,
            "verify_mode": req.eval_at_ms is not None,
            "panels": {},
            "sectors": sectors_payload,
        }

    log.info(
        "/scoring/sector-scan: scoring %d constituents from sectors %s",
        len(constituents), [t.etf for t in tops],
    )
    try:
        full_states = _run_with_timeout(
            lambda: build_states(
                provider,
                constituents,
                today=today_date,
                intraday_lookback_days=intraday_lookback,
                eval_at_ms=req.eval_at_ms,
            ),
            timeout_sec=req.timeout_sec,
        )
    except _ScoringTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except PacingBudgetExhausted as exc:
        retry = max(1, int(round(exc.retry_after_sec)))
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": str(retry)},
        ) from exc
    except Exception as exc:
        log.exception("Constituent fetch failed")
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {exc!r}") from exc

    panels: dict[str, Any] = {}
    for cadence_secs in req.cadences:
        eval_ms = most_recent_cadence_boundary(
            actual_now_ms, cadence_secs, today_session_ms
        )
        runner = ScannerRunner(
            scanner_id=f"sector-{cadence_secs}",
            weights=weights, gates=gates, tier_thresholds=tiers,
            dq_config=DisqualifierConfig(), top_n=req.top_n,
        )
        for state in full_states.values():
            runner.add_symbol(state.truncate_to(eval_ms))
        rankings, rejected = runner.score_all_diagnostic(eval_ms)
        reason_counts: dict[str, int] = {}
        for rej in rejected:
            for reason in rej.reasons:
                tag = reason.split(" ", 1)[0]
                reason_counts[tag] = reason_counts.get(tag, 0) + 1
        panels[str(cadence_secs)] = {
            "cadence_seconds": cadence_secs,
            "eval_ms": eval_ms,
            "n_candidates_scanned": len(full_states),
            "n_results": len(rankings),
            "n_rejected": len(rejected),
            "rankings": [_score_result_to_dict(r) for r in rankings],
            "rejected": [
                {"symbol": rej.symbol, "reasons": list(rej.reasons)}
                for rej in rejected
            ],
            "rejection_summary": [
                {"reason": k, "count": v}
                for k, v in sorted(reason_counts.items(), key=lambda x: -x[1])
            ],
        }

    log.info(
        "/scoring/sector-scan done: %d cadences, %d states, total %.2fs",
        len(panels), len(full_states), time.perf_counter() - t_start,
    )

    return {
        "ran_at": datetime.now(UTC).isoformat(),
        "provider": req.provider,
        "now_ms": actual_now_ms,
        "session_start_ms": today_session_ms,
        "verify_mode": req.eval_at_ms is not None,
        "panels": panels,
        "sectors": sectors_payload,
    }


def _sectors_payload(
    ranked: list[Any],
    tops: list[Any],
    constituents_by_sector: dict[str, list[str]],
) -> dict[str, Any]:
    return {
        "ranked": [r.to_dict() for r in ranked],
        "top_etfs": [t.etf for t in tops],
        "top_names": [t.name for t in tops],
        "constituents_by_sector": constituents_by_sector,
    }


@app.get("/scoring/stream")
async def scoring_stream(
    provider: str = "polygon",
    tickers: str | None = None,
    live: bool = True,
    cadence_seconds: float = 60.0,
    halt_lookback_seconds: float = 120.0,
) -> StreamingResponse:
    """SSE stream of `RankingsUpdate` events from the scoring engine.

    Query params:
      - `provider`: data source (default `polygon`)
      - `tickers`: comma-separated; defaults to the active config watchlist
      - `live`: when true (default) attach the shared Polygon WS so the
        runner state stays current; when false, run cycles against the
        bars seeded at bootstrap (useful for synthetic providers and tests)
      - `cadence_seconds`: how often the runner re-scores. Spec-supported
        values are 60 (1m), 120 (2m), 300 (5m), 900 (15m). The math is
        identical at every cadence; only the refresh interval changes.
        Different (provider, tickers, cadence) tuples each get their own
        runner instance.
    """
    ticker_list = (
        [t.strip().upper() for t in tickers.split(",") if t.strip()]
        if tickers
        else list(_state.config.default_tickers)
    )
    if not ticker_list:
        raise HTTPException(status_code=400, detail="No tickers configured")

    try:
        runner, _key = _state.scoring_runner(
            provider_name=provider,
            tickers=ticker_list,
            attach_live_feed=live and provider == "polygon",
            cadence_seconds=cadence_seconds,
            halt_lookback_seconds=halt_lookback_seconds,
        )
    except Exception as exc:
        log.exception("Scoring runner bootstrap failed")
        raise HTTPException(
            status_code=502, detail=f"Scoring runner bootstrap failed: {exc!r}"
        ) from exc

    runner.start()  # idempotent — no-op if already running
    queue = runner.subscribe()

    async def event_gen() -> AsyncIterator[str]:
        try:
            # Emit one snapshot immediately so the client doesn't have to
            # wait a full cycle for first paint.
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            initial = runner.score_all(now_ms)
            snapshot_payload = {
                "rankings": [_score_result_to_dict(r) for r in initial],
                "cadence_seconds": cadence_seconds,
            }
            yield f"event: snapshot\ndata: {json.dumps(snapshot_payload)}\n\n"
            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=15.0)
                    payload = {
                        "scanner_id": update.scanner_id,
                        "timestamp_ms": update.timestamp_ms,
                        "cadence_seconds": cadence_seconds,
                        "rankings": [_score_result_to_dict(r) for r in update.rankings],
                        "rejected": [
                            {"symbol": rej.symbol, "reasons": list(rej.reasons)}
                            for rej in update.rejected
                        ],
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            runner.unsubscribe(queue)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


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

    # Same liquidity-on-daily wiring as /scan — the constituent universe
    # filter checks a 20-bar volume window, which on intraday means 20
    # minutes / 100 minutes, far below the daily-tuned floors. Without
    # daily_data the entire constituent set fails the universe gate and
    # sector rotation returns 0 stocks even when the top sectors have
    # plenty of breakouts. CachedProvider keeps repeat calls cheap.
    constituent_daily_data: dict[str, pd.DataFrame] | None = None
    if cfg.interval != "1d":
        try:
            constituent_daily_data = provider.get_bars_batch(
                constituent_universe, "1d", 30
            )
        except PacingBudgetExhausted as exc:
            log.warning(
                "Sector-rotation daily liquidity fetch hit pacing budget (%s); "
                "using in-bar fallback (likely returns 0 results on intraday)",
                exc,
            )
        except Exception as exc:
            log.warning("Sector-rotation daily liquidity fetch failed (%s)", exc)

    report = run_sector_rotation(
        sector_data,
        spy_df,
        constituent_data,
        cfg,
        top_n=req.top_n,
        daily_data=constituent_daily_data,
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


def _connection_payload(cfg: ConnectionConfig) -> dict[str, Any]:
    """Render a connection config + live status for the API. Never
    raises — connection probes are best-effort."""
    broker = None
    with _state._broker_lock:
        broker = _state._connections.get(cfg.label)
    accounts: list[str] = []
    paper: bool | None = None
    default_account: str | None = cfg.default_account
    connected = False
    if broker is not None:
        try:
            accounts = list(broker.accounts)
            paper = broker.paper
            default_account = broker.default_account
            # Accounts is populated at first successful connect; treat
            # its presence as "we've got a live socket."
            connected = bool(accounts)
        except Exception:
            pass
    return {
        "label": cfg.label,
        "host": cfg.host,
        "port": cfg.port,
        "client_id": cfg.client_id,
        "paper": cfg.paper if paper is None else paper,
        "auto_connect": cfg.auto_connect,
        "default_account": default_account,
        "accounts": accounts,
        "connected": connected,
    }


@app.get("/broker/connections")
def broker_connections() -> dict[str, Any]:
    """List every configured TWS connection and its live state.

    The frontend uses this to render the connection→accounts picker for
    multi-target order entry. Calling this also lazy-instantiates any
    `auto_connect` brokers that haven't been touched yet.
    """
    return {"connections": [_connection_payload(c) for c in _state.list_connections()]}


@app.post("/broker/connections/{label}/connect")
def broker_connection_connect(label: str) -> dict[str, Any]:
    """Force a connect on the named connection. Returns the same
    payload shape as `/broker/connections` for that one row."""
    cfg = next((c for c in _state.list_connections() if c.label == label), None)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"connection {label!r} not configured")
    b = _state.broker(label)
    if b is None:
        raise HTTPException(status_code=503, detail=f"broker {label!r} unavailable")
    try:
        # Touching get_account triggers ib_async's lazy connect.
        b.get_account()
    except Exception as exc:
        log.warning("broker connect %s failed: %r", label, exc)
        # Don't error — return current state so the UI can show the failure
    return _connection_payload(cfg)


@app.post("/broker/connections/{label}/disconnect")
def broker_connection_disconnect(label: str) -> dict[str, Any]:
    cfg = next((c for c in _state.list_connections() if c.label == label), None)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"connection {label!r} not configured")
    _state.disconnect_label(label)
    return _connection_payload(cfg)


class ConnectionUpsertRequest(BaseModel):
    """Body for `POST /broker/connections` (create) and `PUT
    /broker/connections/{label}` (update). Only `label`, `host`, `port`,
    `client_id` are mandatory."""

    label: str
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 28
    paper: bool = True
    auto_connect: bool = True
    default_account: str | None = None


def _validate_connection_request(
    req: ConnectionUpsertRequest, *, exclude_label: str | None = None,
) -> ConnectionConfig:
    label = req.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label required")
    existing = _state.list_connections()
    for c in existing:
        if c.label == label and c.label != exclude_label:
            raise HTTPException(status_code=409, detail=f"label {label!r} already exists")
        # client_id collisions are silent footguns — IB will boot one
        # client out the next time TWS reconnects. Surface it now.
        if c.client_id == int(req.client_id) and c.label != exclude_label:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"client_id {req.client_id} already used by connection "
                    f"{c.label!r}; pick a different number"
                ),
            )
    return ConnectionConfig(
        label=label,
        host=req.host.strip() or "127.0.0.1",
        port=int(req.port),
        client_id=int(req.client_id),
        paper=bool(req.paper),
        auto_connect=bool(req.auto_connect),
        default_account=(
            req.default_account.strip() if req.default_account else None
        ),
    )


@app.post("/broker/connections")
def broker_connection_create(req: ConnectionUpsertRequest) -> dict[str, Any]:
    """Add a new TWS connection. Persists to `connections.json`. The
    broker is *not* eagerly connected — call POST .../connect after."""
    cfg = _validate_connection_request(req)
    items = _state.list_connections() + [cfg]
    _state._replace_connections(items)
    return _connection_payload(cfg)


@app.put("/broker/connections/{label}")
def broker_connection_update(
    label: str, req: ConnectionUpsertRequest,
) -> dict[str, Any]:
    """Edit a connection's host/port/client_id/etc. If the connection
    is currently up and any field changed, the existing socket is torn
    down so a `POST .../connect` reads the new values."""
    items = _state.list_connections()
    if not any(c.label == label for c in items):
        raise HTTPException(status_code=404, detail=f"connection {label!r} not configured")
    new_cfg = _validate_connection_request(req, exclude_label=label)
    # Force-disconnect the live broker — its `_cfg` is stale.
    _state.disconnect_label(label)
    next_items = [new_cfg if c.label == label else c for c in items]
    # If the label itself was renamed, drop the old entry by label too
    # (already handled if names match, but defensive).
    if new_cfg.label != label:
        next_items = [c for c in next_items if c.label != label] + [new_cfg]
        _state.disconnect_label(label)
    _state._replace_connections(next_items)
    return _connection_payload(new_cfg)


@app.delete("/broker/connections/{label}")
def broker_connection_delete(label: str) -> dict[str, Any]:
    items = _state.list_connections()
    if not any(c.label == label for c in items):
        raise HTTPException(status_code=404, detail=f"connection {label!r} not configured")
    _state.disconnect_label(label)
    _state._replace_connections([c for c in items if c.label != label])
    return {"label": label, "deleted": True}


@app.get("/broker/account-aliases")
def broker_account_aliases() -> dict[str, str]:
    """Map of `account_id → friendly alias`. Edited via PUT below."""
    return load_aliases()


class AliasPutRequest(BaseModel):
    aliases: dict[str, str]


@app.put("/broker/account-aliases")
def broker_account_aliases_put(req: AliasPutRequest) -> dict[str, str]:
    cleaned = {k.strip(): v.strip() for k, v in req.aliases.items() if k and v}
    save_aliases(cleaned)
    return cleaned


@app.get("/broker/accounts-summary")
def broker_accounts_summary(connection: str | None = None) -> dict[str, Any]:
    """One row per IB account across every (or one) connection with the
    headline financial tags. Used by the Connect page's Accounts table.
    Aliases applied server-side so the UI doesn't need a second lookup.
    """
    targets = _brokers_for_query(connection)
    aliases = load_aliases()
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for label, b in targets:
        try:
            for s in b.get_all_account_summaries():
                s["connection"] = label
                s["alias"] = aliases.get(s["account"], "")
                rows.append(s)
        except Exception as exc:
            log.warning("get_all_account_summaries failed for %s: %r", label, exc)
            errors.append({"connection": label, "error": repr(exc)})
    return {"accounts": rows, "errors": errors}


@app.get("/broker/status")
def broker_status() -> dict[str, Any]:
    """Backward-compat status probe.

    Returns the *default* connection's status at the top level (so the
    existing single-connection dashboard keeps working unchanged), plus
    a `connections: [...]` array with details for every configured
    connection — the new multi-connection UI reads from there.

    When `TRADEPILOT_BROKER_DISABLED` is set, returns a disabled-state
    payload so the dashboard can hide broker UI cleanly without polling
    other broker endpoints.
    """
    if _broker_disabled():
        return {
            "connected": False,
            "disabled": True,
            "paper": None,
            "accounts": [],
            "default_account": None,
            "hint": "Broker disabled via TRADEPILOT_BROKER_DISABLED env var.",
            "connections": [],
        }
    configs = _state.list_connections()
    if not configs:
        return {
            "connected": False,
            "paper": None,
            "accounts": [],
            "default_account": None,
            "hint": "No TWS connections configured. Edit data_cache/connections.json.",
            "connections": [],
        }
    b = _state.broker()  # default
    payload: dict[str, Any] = {
        "connected": False,
        "paper": configs[0].paper,
        "account": None,
        "default_account": None,
        "accounts": [],
        "hint": None,
        "connections": [_connection_payload(c) for c in configs],
    }
    if b is not None:
        try:
            payload["paper"] = b.paper
            payload["account"] = b.account
            payload["default_account"] = b.default_account
            payload["accounts"] = b.accounts
            payload["connected"] = True
        except Exception:
            pass
    return payload


@app.get("/broker/account")
def broker_account(connection: str | None = None) -> dict[str, Any]:
    """Account snapshot. Pass `?connection=label` to target a specific
    connection; otherwise the *default* connection's account is returned
    (matches single-connection legacy behavior). Use `/broker/connections`
    to enumerate available connections."""
    b = _state.broker(connection)
    if b is None:
        raise HTTPException(
            status_code=503,
            detail=f"connection {connection!r} unavailable" if connection else "Broker not configured",
        )
    try:
        snap = b.get_account().to_dict()
    except Exception as exc:
        log.exception("IB get_account failed for %s", connection or "default")
        raise HTTPException(status_code=502, detail=f"IB: {exc!r}") from exc
    snap["connection"] = connection or _state._resolve_label(None)
    return snap


@app.get("/broker/positions")
def broker_positions(connection: str | None = None) -> dict[str, Any]:
    """Open positions. Without `?connection=`, aggregates across every
    instantiated broker — each row is tagged with its source `connection`
    so the UI can group/filter."""
    targets = _brokers_for_query(connection)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for label, b in targets:
        try:
            for p in b.get_positions():
                d = p.to_dict()
                d["connection"] = label
                rows.append(d)
        except Exception as exc:
            log.warning("get_positions failed for %s: %r", label, exc)
            errors.append({"connection": label, "error": repr(exc)})
    return {"positions": rows, "errors": errors}


def _brokers_for_query(connection: str | None) -> list[tuple[str, IBBroker]]:
    """Resolve the target list for read endpoints. With an explicit
    connection: just that one (errors if unknown). Without: every
    instantiated broker."""
    if connection is not None:
        b = _state.broker(connection)
        if b is None:
            raise HTTPException(status_code=404, detail=f"connection {connection!r} unavailable")
        return [(connection, b)]
    # Eager-instantiate so a fresh process answers correctly without
    # requiring a prior per-connection request.
    _state.eager_init_auto_connect()
    out = _state.all_brokers()
    if not out:
        raise HTTPException(status_code=503, detail="No connections configured")
    return out


@app.post("/broker/close-all")
def broker_close_all(connection: str | None = None) -> dict[str, Any]:
    """Kill switch. Without `?connection=`, fans out to every connection;
    with one, scopes to that connection only."""
    targets = _brokers_for_query(connection)
    submitted = ok = failed = 0
    details: list[dict[str, Any]] = []
    for label, b in targets:
        try:
            r = b.close_all_positions(cancel_orders=True)
            submitted += int(r.get("submitted", 0))
            ok += int(r.get("ok", 0))
            failed += int(r.get("failed", 0))
            for d in r.get("details", []):
                d["connection"] = label
                details.append(d)
        except Exception as exc:
            log.exception("close_all_positions failed for %s", label)
            failed += 1
            details.append({"connection": label, "error": repr(exc), "ok": False})
    log.warning(
        "Kill switch fired — submitted=%d ok=%d failed=%d across %d connection(s)",
        submitted, ok, failed, len(targets),
    )
    return {"submitted": submitted, "ok": ok, "failed": failed, "details": details}


def _resolve_targets(req: OrderRequest) -> list[OrderTarget]:
    """Normalize the order's destination list.

    Three caller shapes accepted:
      1) `targets=[OrderTarget(...), ...]` (preferred, multi-target)
      2) legacy `connection`/`account` top-level fields  → single target
      3) neither → one target with both fields None (default conn / account)
    """
    if req.targets:
        return list(req.targets)
    if req.connection is not None or req.account is not None:
        return [OrderTarget(connection=req.connection, account=req.account)]
    return [OrderTarget(connection=None, account=None)]


@app.post("/broker/orders")
def broker_submit_order(req: OrderRequest) -> dict[str, Any]:
    """Place a new order. With `targets=[{connection,account}, ...]` the
    server fans out one IB placeOrder per target and returns the list.
    Single-target legacy callers (no `targets`, no `connection`) still
    work and get back a one-element list under the same `orders` key.
    """
    side = req.side.lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    if req.qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be > 0")
    symbol = req.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    targets = _resolve_targets(req)
    if not targets:
        raise HTTPException(status_code=400, detail="targets cannot be empty")

    placed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for tgt in targets:
        # Prefer the explicit connection; otherwise infer from the
        # account if given (so legacy callers passing just `account`
        # auto-route to the right TWS); else default connection.
        label = tgt.connection
        if label is None and tgt.account:
            label = _state.account_to_label(tgt.account)
        b = _state.broker(label)
        if b is None:
            failures.append({
                "connection": label,
                "account": tgt.account,
                "error": f"connection {label!r} unavailable",
            })
            continue

        log.info(
            "Submitting order: conn=%s account=%s paper=%s %s %s qty=%s type=%s tif=%s "
            "limit=%s stop=%s planned_stop=%s score=%s peg_offset=%s cap_price=%s",
            label, tgt.account or b.default_account, b.paper, side, symbol,
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
                account=tgt.account,
            )
        except KillSwitchActive as exc:
            # Hard-fail the whole batch — one connection's circuit breaker
            # firing means we shouldn't quietly half-place a multi-target order.
            raise HTTPException(
                status_code=423,
                detail=str(exc),
                headers={"X-Risk-Locked": "daily_drawdown"},
            ) from exc
        except ValueError as exc:
            failures.append({
                "connection": label, "account": tgt.account, "error": str(exc),
            })
            continue
        except Exception as exc:
            log.exception("IB submit_order failed for %s/%s", label, tgt.account)
            failures.append({
                "connection": label, "account": tgt.account, "error": repr(exc),
            })
            continue

        d = order.to_dict()
        d["connection"] = label
        placed.append(d)

    # Some succeeded, some failed → 207-style payload (still 200 OK so
    # the UI can show what worked). All failed → 502 so the UI flags it.
    if not placed and failures:
        raise HTTPException(status_code=502, detail={"orders": placed, "failures": failures})
    return {"orders": placed, "failures": failures}


@app.delete("/broker/positions/{symbol}")
def broker_close_position(
    symbol: str,
    percentage: float | None = None,
    qty: float | None = None,
    account: str | None = None,
    connection: str | None = None,
) -> dict[str, Any]:
    """Close a single position fully or partially.

    `?percentage=50` for a 50% close, `?qty=5` for a 5-share close, or
    neither for a full close. `?account=` targets a specific account;
    `?connection=` targets a specific connection (auto-resolved from
    `account` if omitted).
    """
    label = connection
    if label is None and account:
        label = _state.account_to_label(account)
    b = _state.broker(label)
    if b is None:
        raise HTTPException(status_code=503, detail="Broker not configured")

    sym = symbol.strip().upper()
    log.info(
        "Close position: conn=%s paper=%s account=%s symbol=%s qty=%s percentage=%s",
        label, b.paper, account or b.default_account, sym, qty, percentage,
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
    d = order.to_dict()
    d["connection"] = label or _state._resolve_label(None)
    return d


@app.delete("/broker/orders")
def broker_cancel_orders(symbol: str, connection: str | None = None) -> dict[str, Any]:
    """Cancel every still-working order for one symbol. With
    `?connection=`, scopes to that connection; otherwise broadcasts to
    every connection (orders only on the originating client are
    cancelled — orders from other connections will be left intact)."""
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol is required")
    targets = _brokers_for_query(connection)
    log.info("Cancel orders for symbol: %s across %d connection(s)", sym, len(targets))
    aggregated: list[dict[str, Any]] = []
    for label, b in targets:
        try:
            r = b.cancel_orders_for_symbol(sym)
            r["connection"] = label
            aggregated.append(r)
        except Exception as exc:
            log.exception("cancel_orders_for_symbol failed for %s", label)
            aggregated.append({"connection": label, "error": repr(exc), "canceled": 0})
    total = sum(int(r.get("canceled", 0)) for r in aggregated)
    return {"symbol": sym, "canceled": total, "results": aggregated}


@app.get("/broker/orders")
def broker_get_orders(
    limit: int = 30,
    status: str = "all",
    connection: str | None = None,
) -> dict[str, Any]:
    """Recent orders. Without `?connection=`, aggregates across every
    instantiated broker — each row is tagged with its source `connection`."""
    targets = _brokers_for_query(connection)
    capped_limit = max(1, min(limit, 200))
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for label, b in targets:
        try:
            for o in b.get_orders(limit=capped_limit, status=status):
                d = o.to_dict()
                d["connection"] = label
                rows.append(d)
        except Exception as exc:
            log.warning("get_orders failed for %s: %r", label, exc)
            errors.append({"connection": label, "error": repr(exc)})
    # Most-recent first across the union; broker.get_orders already
    # returns reverse-chronological per connection, so a sort by
    # submitted_at is a stable interleave.
    rows.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    if connection is None:
        rows = rows[:capped_limit]
    return {"orders": rows, "errors": errors}


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
async def broker_stream(connection: str | None = None) -> StreamingResponse:
    """Server-sent events: pushes account / position / order / fill /
    risk deltas as ib_async events fire across **every** instantiated
    broker (or one, with `?connection=`). Each event carries a
    `connection` field so the UI knows which TWS it came from.

    The initial `event: snapshot` carries one snapshot per active
    connection in `{label: snapshot}` form so a reconnecting client can
    rebuild full multi-connection state in one round trip.
    """
    if connection is not None:
        b = _state.broker(connection)
        if b is None:
            raise HTTPException(status_code=503, detail=f"connection {connection!r} unavailable")
        targets = [(connection, b)]
    else:
        _state.eager_init_auto_connect()
        targets = _state.all_brokers()
        if not targets:
            raise HTTPException(status_code=503, detail="No connections configured")

    # Force a connect on each target so reqAccountUpdates is wired and
    # events flow. Run in parallel with a hard cap — one slow/failing
    # TWS shouldn't keep the SSE stream from opening for the others.
    async def _probe(label: str, b: IBBroker) -> None:
        try:
            await asyncio.wait_for(asyncio.to_thread(b.get_account), timeout=2.0)
        except Exception as exc:
            log.warning("SSE pre-connect failed for %s: %r", label, exc)

    if targets:
        await asyncio.gather(*(_probe(label, b) for label, b in targets))

    loop = asyncio.get_running_loop()
    # Per-connection (queue, sub_id) so we can clean up in finally.
    subs: list[tuple[str, asyncio.Queue, int, Any]] = []
    per_conn_snaps: dict[str, Any] = {}
    for label, b in targets:
        queue, sub_id = b.state.subscribe(loop)
        subs.append((label, queue, sub_id, b))
        snap = b.state.snapshot()
        # Tag each position/order row in the snapshot with its source
        # connection so the UI can key state by `(connection, account, …)`
        # without a separate lookup. BrokerState doesn't know its own
        # label, so the SSE layer is the right place to do this.
        for p in snap.get("positions", []):
            if isinstance(p, dict) and "connection" not in p:
                p["connection"] = label
        for o in snap.get("orders", []):
            if isinstance(o, dict) and "connection" not in o:
                o["connection"] = label
        per_conn_snaps[label] = snap

    # Backward-compatible snapshot envelope: top-level keys mirror the
    # default connection's snapshot (so legacy single-connection clients
    # see the same shape they always did), with a `connections: {label:
    # snap}` map alongside for multi-connection-aware UIs.
    default_label = _state._resolve_label(None) or ""
    default_snap = per_conn_snaps.get(default_label, {})
    snapshot_payload = {**default_snap, "connections": per_conn_snaps}

    async def event_gen() -> AsyncIterator[str]:
        # Fan-in: one task per source queue draining into a shared queue.
        merged: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1024)

        async def drain(label: str, q: asyncio.Queue) -> None:
            while True:
                ev = await q.get()
                await merged.put((label, ev))

        drain_tasks = [asyncio.create_task(drain(label, q)) for label, q, _, _ in subs]
        try:
            yield f"event: snapshot\ndata: {json.dumps(snapshot_payload)}\n\n"
            while True:
                try:
                    label, ev = await asyncio.wait_for(
                        merged.get(), timeout=_SSE_HEARTBEAT_SEC
                    )
                    # Tag the event so the UI can route it to the right
                    # row. We tag both at the top level (`ev.connection`)
                    # and inside the payload (`ev.payload.connection`)
                    # so consumers can key state by `(connection, …)`
                    # without a separate lookup either way.
                    if isinstance(ev, dict):
                        payload = ev.get("payload")
                        if isinstance(payload, dict) and "connection" not in payload:
                            payload = {**payload, "connection": label}
                            ev = {**ev, "payload": payload, "connection": label}
                        else:
                            ev = {**ev, "connection": label}
                    else:
                        ev = {"connection": label, "payload": ev}
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            for t in drain_tasks:
                t.cancel()
            for label, _, sub_id, b in subs:
                try:
                    b.state.unsubscribe(sub_id)
                except Exception:
                    log.debug("unsubscribe failed for %s", label, exc_info=True)

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


# symbol -> (cached_at_monotonic, prior_close). Daily bars only roll over once
# a day, so a 30-minute TTL is generous and lets dashboard reconnects share.
_PRIOR_CLOSE_CACHE: dict[str, tuple[float, float]] = {}
_PRIOR_CLOSE_TTL_SEC = 30 * 60.0
_PRIOR_CLOSE_LOCK = threading.Lock()


def _prior_close_for(symbol: str) -> float | None:
    """Most recent regular-session daily close strictly before today.

    Pulled via the same Polygon CachedProvider the scanner uses, so the
    parquet cache is shared. Returns None on any failure (no Polygon key,
    rate limit, network) — callers must tolerate the missing field.
    """
    now = time.monotonic()
    with _PRIOR_CLOSE_LOCK:
        hit = _PRIOR_CLOSE_CACHE.get(symbol)
        if hit is not None and (now - hit[0]) < _PRIOR_CLOSE_TTL_SEC:
            return hit[1]
    try:
        provider = _state.provider("polygon")
    except (ValueError, RuntimeError):
        return None
    try:
        df = provider.get_bars(symbol, "1d", 5)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    today = datetime.now(UTC).date()
    # Drop today's running aggregate if Polygon emitted it; we want the prior
    # session's close, not whatever fragment of today is sitting in there.
    try:
        eligible = df[df.index.date < today]
    except (AttributeError, TypeError):
        eligible = df
    if eligible.empty:
        eligible = df
    try:
        prior = float(eligible["close"].iloc[-1])
    except (KeyError, IndexError, ValueError):
        return None
    with _PRIOR_CLOSE_LOCK:
        _PRIOR_CLOSE_CACHE[symbol] = (now, prior)
    return prior


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

    # Most recent regular-session daily close strictly before today; the
    # client uses this to compute `change_pct` against live prices without
    # a separate REST round-trip. Failures here don't block the stream.
    # Run via asyncio.to_thread so the synchronous Polygon REST call doesn't
    # block the event loop — the watchlist opens ~20 SSE connections at once
    # on a fresh page load, and serializing all those through the loop
    # freezes every other endpoint until the cache warms up.
    prior_close = await asyncio.to_thread(_prior_close_for, sym)

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
            connected_payload: dict[str, Any] = {"symbol": sym}
            if prior_close is not None:
                connected_payload["prior_close"] = prior_close
            yield f"event: connected\ndata: {json.dumps(connected_payload)}\n\n"
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


@app.get("/quotes/stream-multi")
async def quotes_stream_multi(symbols: str) -> StreamingResponse:
    """Multiplexed SSE for multiple symbols.

    The single-symbol endpoint above forces the browser to open one
    EventSource per ticker. With a watchlist of 20 top picks plus a chart,
    the dashboard quickly hit Chrome's HTTP/1.1 cap of 6 connections per
    origin and queued every subsequent fetch — `/quotes/history` and
    `/scan` would silently sit pending.

    This endpoint accepts a comma-separated list of symbols, opens one
    Polygon WS subscription per symbol upstream, and folds every event
    into a single SSE stream tagged with `symbol`. The dashboard's
    quoteStream hub holds exactly one EventSource regardless of how many
    components subscribe.
    """
    sym_list = sorted({s.strip().upper() for s in symbols.split(",") if s.strip()})
    if not sym_list:
        raise HTTPException(status_code=400, detail="symbols required")
    if len(sym_list) > 100:
        raise HTTPException(status_code=400, detail="too many symbols (max 100)")

    try:
        streamer = _state.quote_streamer()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Parallel prior-close lookup off the event loop — see the comment in
    # quotes_stream for why this matters with bursty subscriptions.
    prior_close_results = await asyncio.gather(
        *[asyncio.to_thread(_prior_close_for, s) for s in sym_list]
    )
    prior_closes = {
        s: pc
        for s, pc in zip(sym_list, prior_close_results, strict=True)
        if pc is not None
    }

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=4096)
    # Per-symbol throttle so a chatty name (think NVDA at the open) can't
    # crowd out events for slower names — each symbol gets its own ~10/sec
    # budget identical to the single-symbol stream.
    last_trade_per_symbol: dict[str, float] = {}

    def make_callback(symbol: str) -> Callable[[dict[str, Any]], None]:
        def on_event(ev: dict[str, Any]) -> None:
            if ev.get("ev") == "T":
                now = time.monotonic()
                if now - last_trade_per_symbol.get(symbol, 0.0) < _TRADE_THROTTLE_SEC:
                    return
                last_trade_per_symbol[symbol] = now
            try:
                loop.call_soon_threadsafe(_safe_put_pair, queue, (symbol, ev))
            except RuntimeError:
                pass

        return on_event

    sub_ids: list[int] = []
    for symbol in sym_list:
        try:
            sid = streamer.subscribe(
                symbol, channels=("AM", "T"), callback=make_callback(symbol)
            )
            sub_ids.append(sid)
        except Exception:
            log.warning("multi-stream subscribe failed for %s", symbol, exc_info=True)

    async def event_gen() -> AsyncIterator[str]:
        try:
            connected_payload = {
                "symbols": sym_list,
                "prior_closes": prior_closes,
            }
            yield f"event: connected\ndata: {json.dumps(connected_payload)}\n\n"
            while True:
                try:
                    symbol, ev = await asyncio.wait_for(
                        queue.get(), timeout=_QUOTES_HEARTBEAT_SEC
                    )
                    payload = _polygon_event_to_payload(ev)
                    if payload is None:
                        continue
                    payload["symbol"] = symbol
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            for sid in sub_ids:
                try:
                    streamer.unsubscribe(sid)
                except Exception:
                    log.debug("multi-stream unsubscribe raised", exc_info=True)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _safe_put_pair(
    queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    item: tuple[str, dict[str, Any]],
) -> None:
    """Drop-oldest variant for the multi-symbol stream's (symbol, event) queue."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
            queue.put_nowait(item)
        except Exception:
            pass


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
    before_ts: int | None = None,
) -> dict[str, Any]:
    """Initial chart bars from Polygon REST. The dashboard's
    RealtimeChart calls this once on mount and then layers on the SSE
    stream for updates. We always go through Polygon here — TradingView
    rendered any provider before, but live updates only come from
    Polygon, so the history must match.

    When `before_ts` is supplied, the window is `[end - lookback_days, end]`
    where `end = date(before_ts)`. This is the lazy-load-on-scroll path:
    the chart pages older bars in as the user pans left, asking only for
    the slice it doesn't already have rather than refetching from today.
    Without `before_ts`, the window is `[today - lookback_days, today]`
    (the default mount-time fetch).
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
        if before_ts is not None:
            end_dt = datetime.fromtimestamp(int(before_ts), UTC).date()
            start_dt = end_dt - timedelta(days=capped_lookback)
            df = provider.get_bars_range(sym, interval, start_dt, end_dt)
        else:
            df = provider.get_bars(sym, interval, capped_lookback)
    except PacingBudgetExhausted as exc:
        retry = max(1, int(round(exc.retry_after_sec)))
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(retry)},
        ) from exc
    except ValueError as exc:
        # Polygon returned 200 with no bars — common when paging past the
        # ticker's listing date or into a market-closed weekend slice.
        # Treat as an empty page rather than 502 so the chart can stop
        # paging without surfacing a scary error.
        log.info("quotes_history empty page for %s: %s", sym, exc)
        return {
            "symbol": sym,
            "interval": interval,
            "lookback_days": capped_lookback,
            "before_ts": before_ts,
            "bars": [],
        }
    except Exception as exc:
        log.warning(
            "quotes_history failed for %s (%s): %s",
            sym, type(exc).__name__, exc,
        )
        raise HTTPException(
            status_code=502,
            detail=f"history fetch failed ({type(exc).__name__}): {exc}",
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
        "before_ts": before_ts,
        "bars": bars,
    }
