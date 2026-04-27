"""FastAPI server exposing scan + backtest + config endpoints.

CORS is open for the Vite dev server at http://localhost:5173. `/scan` results
are cached in-process for 60 seconds so dashboard polling is cheap. Providers
themselves are wrapped with a parquet-backed `CachedProvider` so OHLCV bars
persist across restarts — subsequent scans re-fetch only the delta from the
wrapped provider, saving IB pacing budget.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from scanner.backtest import backtest
from scanner.config import ScannerConfig
from scanner.data import MarketDataProvider, get_provider
from scanner.data.ib_provider import PacingBudgetExhausted
from scanner.engine import scan

log = logging.getLogger(__name__)

app = FastAPI(title="Bullish Scanner API", version="0.1.0")

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
    provider: str = Field(default="synthetic")
    tickers: list[str] | None = None
    interval: str | None = None
    lookback_days: int | None = None


class BacktestRequest(BaseModel):
    provider: str = Field(default="synthetic")
    tickers: list[str] | None = None
    interval: str | None = None
    lookback_days: int | None = None
    holding_bars: int = 3
    target_pct: float = 2.0
    min_history: int = 30


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


@app.post("/scan")
def post_scan(req: ScanRequest) -> dict[str, Any]:
    cfg = _resolve_cfg(req)
    tickers = [t.upper() for t in req.tickers] if req.tickers else list(cfg.default_tickers)
    cache_key = f"{req.provider}|{cfg.interval}|{cfg.lookback_days}|{','.join(sorted(tickers))}"

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
