# TradePilot

A local trading workstation for day traders — directional (long/short) scanner, walk-forward backtests, threshold tuning, IBKR broker integration with manual order entry (semi-auto and auto modes coming), and a live React dashboard. Free data via Polygon, yfinance, and a deterministic synthetic provider for offline work.

## Architecture

```
┌────────────┐   ┌────────────┐   ┌────────────┐
│  Dashboard │──▶│  FastAPI   │──▶│  Scanner   │
│  (Vite+TS) │   │  :8787     │   │  engine    │
│  :5173     │   │  /scan     │   │  + signals │
└────────────┘   │  /backtest │   │  + indic.  │
                 │  /config   │   └──────┬─────┘
                 └────────────┘          │
                                         ▼
                                 ┌───────────────┐
                                 │  Data layer   │
                                 │  yfinance /   │
                                 │  synthetic    │
                                 │  + parquet    │
                                 │  cache        │
                                 └───────────────┘
```

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run.sh test                           # 63 tests, ~30s
./run.sh scan --provider synthetic      # offline ranked list → output/scan_results.json
./run.sh backtest --provider synthetic  # walk-forward report  → output/backtest_report.json
./run.sh api &                          # FastAPI on :8787
(cd dashboard && npm install && npm run dev)  # dashboard on :5173
```

## CLI

| Command | What it does |
| --- | --- |
| `./run.sh scan`     | Rank tickers by bullish score; writes `output/scan_results.json`. |
| `./run.sh backtest` | Replay signals historically; writes `backtest_report.json` + `equity_curve.csv`. |
| `./run.sh tune`     | Grid-search thresholds against a backtest metric; writes `tuning_results.csv`. |
| `./run.sh api`      | Boot the FastAPI server on `:8787`. |
| `./run.sh dash`     | Boot the Vite dev server on `:5173`. |
| `./run.sh test`     | `pytest` with coverage on `scanner/`. |

Common flags: `--provider yfinance|synthetic`, `--tickers AAPL,MSFT`, `--interval 1d|1h|15m|5m|1m`, `--lookback 90`, `--notify console,slack,discord`.

## Tuning

`./run.sh tune --provider synthetic --metric profit_factor` runs an exhaustive grid over `min_relative_volume`, `rsi_min`, `rsi_max`, `breakout_proximity_pct` with a 70/30 date-based train/test split. Watch for the loud `Overfit alarm:` warnings — they fire when out-of-sample performance drops below half of in-sample. For real tuning use a longer lookback (e.g. `--lookback 400`) so the 30% test window has enough bars for the scanner warmup.

## Data providers

Three implementations ship, all behind the `MarketDataProvider` ABC in `scanner/data/base.py`:

- **`synthetic`** — deterministic GBM, offline, stable across runs. Dev default.
- **`yfinance`** — free, ~15-minute delayed, occasional flakiness.
- **`ibkr`** — Interactive Brokers TWS / Gateway via `ib_async`. Real-time if you have the subscriptions.

### Using IBKR

Requires TWS or IB Gateway running with API access enabled. The provider connects read-only (`readonly=True`) so it can never place orders.

```bash
./run.sh scan --provider ibkr --tickers AAPL,MSFT,NVDA --lookback 60
# or with a non-default port:
./run.sh scan --provider ibkr --ib-port 7497 --ib-client-id 17
# equivalent env vars: IB_HOST, IB_PORT, IB_CLIENT_ID
```

**Pacing.** IB bans clients that exceed ~60 historical-data requests per 10-minute sliding window. `IBProvider` enforces a conservative **55 requests / 600 s** cap with a 250 ms spacing between requests via `_SlidingWindowLimiter` in `scanner/data/ib_provider.py`. A full 49-ticker universe fits in one window; a larger universe transparently pauses for up to ~10 minutes rather than get you banned. Check current utilization via `provider.pacing_status()`.

### Day-trading workflow (intraday via IBKR)

The defaults in `ScannerConfig` are calibrated for daily bars. For intraday bars the per-bar volume/dollar-volume floors need to be ~75–100× smaller (a 5m bar has far less volume than a whole day). Two presets ship:

- `configs/intraday_5m.json` — 5-minute bars, 5-day lookback, 42 liquid tickers.
- `configs/intraday_1m.json` — 1-minute bars, 1-day lookback (IB's max per request), 19 most liquid.

**CLI during market hours (9:30 AM – 4:00 PM ET):**

```bash
# single scan — call this whenever you want a fresh read
./run.sh scan --provider ibkr --config configs/intraday_5m.json

# re-run every 5 minutes with standard `watch` on macOS / Linux
watch -n 300 './run.sh scan --provider ibkr --config configs/intraday_5m.json'
```

**From the dashboard:**

1. `./run.sh api` in one terminal, `./run.sh dash` in another. Leave TWS running on port 8401.
2. In the dashboard header, pick `IBKR (TWS)`, then `5m` (or `1m`), lookback `5` (or `1`).
3. Click **Run Scan**. The results cache for 60 seconds server-side, so spamming the button won't hammer IB.

**IB pacing budget for polling.** IB allows 60 requests / 10 min. Your watchlist size × poll frequency must fit:

| Watchlist size | Safe 5m poll interval | Safe 1m poll interval |
|---:|---|---|
| 10  | every 100 s | every 100 s |
| 20  | every 200 s | every 200 s (once per minute would exceed the budget) |
| 40+ | every 400 s | use smaller watchlist |

Stay under the limit or IB will ban the client for ~10 minutes. The built-in limiter will auto-sleep if you push past, but it's better not to.

**Things that are automatically right for intraday:**

- `VWAP` resets per session — `scanner/indicators.py:45`.
- `useRTH=True` in `IBProvider` — no pre-market / after-hours noise polluting the bars.
- API caches `/scan` for 60s — shields IB from dashboard polling.

**Things that aren't live yet:**

- No auto-refresh on the dashboard — you still click "Run Scan" manually. Add a `setInterval` in `App.tsx` if you want auto-poll.
- No order placement. IB provider is read-only by design.
- No news / catalyst overlay (stretch goal in `BUILD.md §16`).

### Adding a new provider

Implement `get_bars(ticker, interval, lookback_days)` and `get_bars_batch(...)`, return OHLCV frames with lowercase columns and a tz-aware sorted `DatetimeIndex`, register the name in `scanner/data/__init__.get_provider`.

## Configuration

All tunable parameters live in a single Pydantic model — see `scanner/config.py`. Round-trip via `ScannerConfig.from_json(path)` / `.to_json(path)`. Scoring weights (the five signals) must sum to 1.0, enforced by a validator.

The five signals the scanner scores, in weight order:

1. **Relative volume** (0.30) — today's volume vs 20-day average.
2. **Momentum** (0.25) — close-to-close %Δ.
3. **Trend alignment** (0.20) — price above VWAP, above EMA9, EMAs 9 > 20 > 50.
4. **Breakout proximity** (0.15) — distance below 20-day high.
5. **RSI position** (0.10) — triangular peak inside `[rsi_min, rsi_max]`, overbought fade to RSI 85.

Hard filter gates (configurable): require-above-VWAP, require-above-EMA9, require-EMA9-above-EMA20, max gap percentage.

## Quality gates

- `./run.sh test` — 63 tests, coverage ≥ 70% on `scanner/` (81% at time of writing).
- `mypy --strict scanner/` — clean.
- `ruff check .` — clean.
- Look-ahead-bias invariant tested in `tests/test_backtest.py::test_no_lookahead_bias`.

## Disclaimers

Day trading is hard. **Most participants lose money.** Backtests on free end-of-day data are optimistic relative to live intraday execution — no slippage, no partial fills, no borrow costs, no tax drag, no psychological friction. This project is a research tool and not financial advice. Do not run with real capital without first wiring a paper-trading harness (see the stretch goals in `BUILD.md §16`) and running it long enough to see an adverse regime.
