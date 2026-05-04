"""Polygon.io (a.k.a. massive.com) MarketDataProvider.

Wraps Polygon's Aggregates v2 endpoint:

    GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}

One HTTP request per ticker — Polygon has no multi-ticker batch endpoint
for aggregates, so `get_bars_batch` just loops `get_bars` under a
sliding-window rate limiter. The limiter defaults to 5 requests/minute,
which matches the free Stocks tier; paid tiers can raise it via
`POLYGON_RATE_LIMIT_PER_MIN` (or just set it very high on Stocks
Advanced where there is effectively no per-minute limit).

Free-tier caveats:
  - 15-minute-delayed end-of-day data only. Intraday bars (1m / 5m / 15m
    / 1h) are paid-tier features. Free users requesting intraday
    intervals get an empty `results` array.
  - Two years of historical depth.

The 429 response carries a `Retry-After` header which we honor; on any
other transient HTTP error we retry up to 3 times with exponential
backoff. Permanent errors (404 for an unknown ticker, 401 for a bad
key) raise immediately.

Env:
  - POLYGON_API_KEY            (required; signs every request)
  - POLYGON_API_BASE           (optional; default https://api.polygon.io —
                                override if/when the rebrand to
                                massive.com flips the host)
  - POLYGON_RATE_LIMIT_PER_MIN (default 5; raise on paid tiers)
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pandas as pd

from scanner.data.base import OHLCV_COLUMNS, MarketDataProvider, validate_bars
from scanner.diagnostics import Warning, warnings as _warning_bus

log = logging.getLogger(__name__)


# Map our canonical interval strings to Polygon's (multiplier, timespan).
_INTERVAL_TO_AGG: dict[str, tuple[int, str]] = {
    "1m": (1, "minute"),
    "2m": (2, "minute"),
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "1h": (1, "hour"),
    "1d": (1, "day"),
}


class PolygonAuthError(RuntimeError):
    """Raised when Polygon returns 401/403 — bad or unauthorized key."""


class PolygonRateLimited(RuntimeError):
    """Raised after we exhaust 429 retries. Carries the last Retry-After."""

    def __init__(self, message: str, retry_after_sec: float) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


@dataclass
class PolygonConfig:
    api_key: str
    base_url: str = "https://api.polygon.io"
    rate_limit_per_min: int = 5
    request_timeout_sec: float = 20.0
    max_retries: int = 3
    # Parallel HTTP fan-out for `get_bars_batch`. Default 16 keeps a
    # 280-ticker sector-rotation pull around ~5s while staying well under
    # both Polygon's per-minute budget and httpx.Client's default 100-conn
    # pool. Drop to 1 to force sequential (e.g. when debugging).
    max_concurrency: int = 16

    @classmethod
    def from_env(cls) -> PolygonConfig:
        key = os.environ.get("POLYGON_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "POLYGON_API_KEY is not set. Sign up at polygon.io (or massive.com) "
                "for a free key, then export POLYGON_API_KEY=..."
            )
        return cls(
            api_key=key,
            base_url=os.environ.get("POLYGON_API_BASE", "https://api.polygon.io"),
            rate_limit_per_min=int(os.environ.get("POLYGON_RATE_LIMIT_PER_MIN", "5")),
            max_concurrency=max(1, int(os.environ.get("POLYGON_MAX_CONCURRENCY", "16"))),
        )


class _MinuteLimiter:
    """Sliding 60-second-window limiter. `acquire()` blocks until a slot
    is free — every call records its timestamp, and the next call sleeps
    long enough that the oldest timestamp falls out of the window.

    Thread-safe; the same client may be hit from FastAPI threads + a
    background scan worker without coordination.
    """

    def __init__(self, max_per_window: int, window_sec: float = 60.0) -> None:
        self._max = max_per_window
        self._window = window_sec
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self._max <= 0:
            return  # disabled
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - self._window
                while self._stamps and self._stamps[0] < cutoff:
                    self._stamps.popleft()
                if len(self._stamps) < self._max:
                    self._stamps.append(now)
                    return
                wait = self._stamps[0] + self._window - now
            if wait > 0:
                time.sleep(min(wait, 5.0))


def _normalize_aggregates(payload: Any, ticker: str) -> pd.DataFrame | None:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not results:
        return None

    df = pd.DataFrame(results)
    needed = {"o", "h", "l", "c", "v", "t"}
    if not needed.issubset(df.columns):
        log.warning(
            "Polygon aggregates missing fields for %s: have %s",
            ticker, list(df.columns),
        )
        return None

    df = df.rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    df = df[list(OHLCV_COLUMNS) + ["t"]].copy()
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("t").rename_axis(None)
    df = df[list(OHLCV_COLUMNS)].dropna()
    if df.empty:
        return None
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


class PolygonProvider(MarketDataProvider):
    """Polygon.io OHLCV adapter. One HTTP call per ticker."""

    def __init__(self, config: PolygonConfig | None = None) -> None:
        self._cfg = config or PolygonConfig.from_env()
        self._limiter = _MinuteLimiter(self._cfg.rate_limit_per_min)
        self._client = httpx.Client(
            base_url=self._cfg.base_url,
            timeout=self._cfg.request_timeout_sec,
            params={"apiKey": self._cfg.api_key},
        )

    def __del__(self) -> None:  # pragma: no cover
        try:
            self._client.close()
        except Exception:
            pass

    def disconnect(self) -> None:
        """Compatible with the broker/provider lifecycle in api/server.py."""
        try:
            self._client.close()
        except Exception:
            log.debug("Polygon client close raised", exc_info=True)

    # ------------------------------------------------------------------
    # MarketDataProvider
    # ------------------------------------------------------------------

    def get_bars(self, ticker: str, interval: str, lookback_days: int) -> pd.DataFrame:
        if interval not in _INTERVAL_TO_AGG:
            raise ValueError(
                f"Unsupported interval {interval!r}; expected one of "
                f"{list(_INTERVAL_TO_AGG)}"
            )
        multiplier, timespan = _INTERVAL_TO_AGG[interval]
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=max(1, lookback_days))

        path = (
            f"/v2/aggs/ticker/{ticker.upper()}/range/{multiplier}/{timespan}/"
            f"{start.isoformat()}/{end.isoformat()}"
        )
        payload = self._request(
            path,
            params={"adjusted": "true", "sort": "asc", "limit": 50000},
            ticker=ticker,
        )
        df = _normalize_aggregates(payload, ticker)
        if df is None:
            raise ValueError(f"Polygon returned no data for {ticker}")
        return validate_bars(df)

    def get_bars_batch(
        self, tickers: list[str], interval: str, lookback_days: int
    ) -> dict[str, pd.DataFrame]:
        if not tickers:
            return {}
        out: dict[str, pd.DataFrame] = {}
        workers = min(self._cfg.max_concurrency, len(tickers))
        if workers <= 1:
            for ticker in tickers:
                try:
                    out[ticker] = self.get_bars(ticker, interval, lookback_days)
                except (PolygonAuthError, PolygonRateLimited):
                    raise
                except Exception as exc:
                    log.warning("Polygon get_bars failed for %s: %s", ticker, exc)
            return out

        # Parallel fan-out: httpx.Client is thread-safe and the rate
        # limiter coordinates across threads, so we just fire workers in
        # parallel. Auth/rate errors short-circuit the whole batch — they
        # won't fix themselves between tickers.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="polygon-batch",
        ) as ex:
            futures = {
                ex.submit(self.get_bars, t, interval, lookback_days): t
                for t in tickers
            }
            try:
                for fut in concurrent.futures.as_completed(futures):
                    t = futures[fut]
                    try:
                        out[t] = fut.result()
                    except (PolygonAuthError, PolygonRateLimited):
                        raise
                    except Exception as exc:
                        log.warning("Polygon get_bars failed for %s: %s", t, exc)
            except (PolygonAuthError, PolygonRateLimited):
                # Cancel queued futures so the executor's shutdown only
                # waits on the ~workers in-flight requests.
                for f in futures:
                    f.cancel()
                raise
        return out

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _request(
        self, path: str, *, params: dict[str, Any], ticker: str
    ) -> dict[str, Any]:
        last_retry_after: float = 0.0
        for attempt in range(self._cfg.max_retries + 1):
            self._limiter.acquire()
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                if attempt >= self._cfg.max_retries:
                    raise
                backoff = 0.5 * (2 ** attempt)
                log.warning(
                    "Polygon HTTP error for %s (attempt %d/%d): %s; backing off %.1fs",
                    ticker, attempt + 1, self._cfg.max_retries + 1, exc, backoff,
                )
                time.sleep(backoff)
                continue

            if resp.status_code in (401, 403):
                raise PolygonAuthError(
                    f"Polygon auth failed ({resp.status_code}). Check POLYGON_API_KEY."
                )
            if resp.status_code == 429:
                last_retry_after = float(resp.headers.get("Retry-After", "1") or 1)
                # Surface to the dashboard regardless of whether we end
                # up retrying successfully — the user wants to see when
                # we're hitting the per-minute ceiling so they can
                # tune POLYGON_RATE_LIMIT_PER_MIN or upgrade the plan.
                _warning_bus.publish(
                    Warning(
                        kind="polygon_rate_limit",
                        message=(
                            f"Polygon rate limit hit for {ticker} "
                            f"(retry after {last_retry_after:.1f}s, "
                            f"attempt {attempt + 1}/{self._cfg.max_retries + 1})"
                        ),
                        detail={
                            "ticker": ticker,
                            "retry_after_sec": last_retry_after,
                            "attempt": attempt + 1,
                            "max_attempts": self._cfg.max_retries + 1,
                            "rate_limit_per_min": self._cfg.rate_limit_per_min,
                        },
                    )
                )
                if attempt >= self._cfg.max_retries:
                    raise PolygonRateLimited(
                        f"Polygon rate-limited after {self._cfg.max_retries + 1} attempts",
                        retry_after_sec=last_retry_after,
                    )
                log.info(
                    "Polygon 429 for %s; sleeping %.1fs (Retry-After)",
                    ticker, last_retry_after,
                )
                time.sleep(last_retry_after)
                continue
            if resp.status_code >= 500:
                if attempt >= self._cfg.max_retries:
                    resp.raise_for_status()
                backoff = 0.5 * (2 ** attempt)
                log.warning(
                    "Polygon %d for %s; retrying in %.1fs",
                    resp.status_code, ticker, backoff,
                )
                time.sleep(backoff)
                continue
            resp.raise_for_status()
            return resp.json()

        # Exhausted retries without returning — last response was 429 or 5xx.
        raise PolygonRateLimited(
            "Polygon retries exhausted",
            retry_after_sec=last_retry_after,
        )
