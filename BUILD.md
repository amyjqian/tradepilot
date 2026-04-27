# Build Spec — Bullish Stock Scanner for Day Traders

**Audience:** Claude Code (or any capable coding agent).
**Goal:** Build a production-quality stock scanner + backtester + dashboard from this spec.
**Deliverable:** A runnable local project the user can `pip install -r requirements.txt && ./run.sh` and get (a) a ranked list of bullish stocks, (b) a backtest report, (c) a live web dashboard.

Follow this document top-to-bottom. Each section ends with a **verification step** you must run before moving on. Do not skip verification steps.

---

## 0. Ground rules

1. **No broker connection in v1.** Use free data (yfinance) + a synthetic provider for offline testing. The architecture must make it trivial to swap in Polygon/Alpaca later.
2. **Type-hint everything.** Use `from __future__ import annotations` and `mypy --strict` should pass on the `scanner/` package.
3. **Test as you go.** Every module gets a matching test file. Run `pytest` before claiming a section is done.
4. **Never silently catch exceptions.** Log and re-raise, or log and skip with a visible warning.
5. **The scanner must be backtestable.** Any signal logic that can't be replayed on historical data is rejected.
6. **Idempotent and deterministic.** Same inputs → same outputs. No hidden clock/random state in the scanner logic itself.

---

## 1. Project layout

Create exactly this structure:

```
bullish_scanner/
├── README.md
├── requirements.txt
├── pyproject.toml
├── run.sh                      # convenience launcher
├── scanner/                    # core Python package
│   ├── __init__.py
│   ├── config.py               # all tunable params
│   ├── data/
│   │   ├── __init__.py
│   │   ├── base.py             # MarketDataProvider ABC
│   │   ├── yfinance_provider.py
│   │   ├── synthetic_provider.py
│   │   └── cache.py            # on-disk parquet cache
│   ├── indicators.py           # EMA, RSI, VWAP, ATR, rel-vol, etc.
│   ├── signals.py              # individual bullish signal evaluators
│   ├── engine.py               # scan() — orchestrator
│   ├── backtest.py             # walk-forward validator
│   ├── tuning.py               # grid-search over thresholds
│   └── alerts.py               # Slack/Discord/console notifiers
├── api/
│   ├── __init__.py
│   └── server.py               # FastAPI server exposing /scan, /backtest
├── dashboard/                  # React + Vite frontend
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── api.ts
│       ├── components/
│       │   ├── ScanTable.tsx
│       │   ├── ScoreBreakdown.tsx
│       │   ├── BacktestSummary.tsx
│       │   ├── TradeScatter.tsx
│       │   └── EquityCurve.tsx
│       └── styles.css
├── tests/
│   ├── test_indicators.py
│   ├── test_signals.py
│   ├── test_engine.py
│   ├── test_backtest.py
│   └── test_data.py
└── scripts/
    ├── run_scan.py
    ├── run_backtest.py
    └── run_tuning.py
```

**Verification:** `tree bullish_scanner -L 2` matches the above. `touch` empty files for now; they'll get content in subsequent sections.

---

## 2. Dependencies

`requirements.txt`:
```
pandas>=2.1
numpy>=1.26
yfinance>=0.2.40
pydantic>=2.5
fastapi>=0.110
uvicorn[standard]>=0.27
pyarrow>=15          # parquet cache
httpx>=0.27
rich>=13             # pretty CLI output
pytest>=8
pytest-cov>=4
mypy>=1.8
ruff>=0.3
```

`pyproject.toml` must configure ruff (line length 100) and mypy (strict on `scanner/`).

**Verification:** `pip install -r requirements.txt` succeeds in a fresh venv.

---

## 3. Configuration (`scanner/config.py`)

Use Pydantic `BaseModel` (not dataclasses) so config can round-trip through JSON for the tuner.

Required fields:

**`UniverseConfig`**
- `min_price: float = 2.0`
- `max_price: float = 500.0`
- `min_avg_volume: int = 500_000`   # 20-day average
- `min_dollar_volume: float = 10_000_000`   # price × volume floor

**`SignalThresholds`**
- `min_relative_volume: float = 1.5`
- `min_gap_pct: float = 1.0`
- `max_gap_pct: float = 15.0`   # extreme gaps often fade
- `rsi_min: float = 50.0`
- `rsi_max: float = 70.0`
- `breakout_proximity_pct: float = 0.5`   # within 0.5% of 20-day high
- `require_above_vwap: bool = True`
- `require_above_ema9: bool = True`
- `require_ema9_above_ema20: bool = True`

**`ScoringWeights`** (must sum to 1.0; validate in `@model_validator`)
- `relative_volume: float = 0.30`
- `momentum: float = 0.25`
- `trend_alignment: float = 0.20`
- `rsi_position: float = 0.10`
- `breakout_proximity: float = 0.15`

**`ScannerConfig`** — composes the above, plus:
- `default_tickers: list[str]` — seed with ~50 liquid large/mid-caps across sectors (mega-cap tech, semis, financials, consumer, healthcare, industrials, high-beta names).
- `top_n_results: int = 20`
- `interval: Literal["1d","1h","15m","5m","1m"] = "1d"`
- `lookback_days: int = 90`

Provide `ScannerConfig.from_json(path)` and `.to_json(path)` helpers.

**Verification:** `python -c "from scanner.config import ScannerConfig; print(ScannerConfig().model_dump_json(indent=2))"` prints a valid config.

---

## 4. Data layer (`scanner/data/`)

### 4.1 `base.py` — interface

```python
class MarketDataProvider(ABC):
    @abstractmethod
    def get_bars(self, ticker: str, interval: str, lookback_days: int) -> pd.DataFrame: ...

    @abstractmethod
    def get_bars_batch(self, tickers: list[str], interval: str, lookback_days: int
                       ) -> dict[str, pd.DataFrame]: ...
```

Returned DataFrames must have columns `[open, high, low, close, volume]` with a tz-aware `DatetimeIndex` sorted ascending. Reject anything else in a validator helper `validate_bars(df)`.

### 4.2 `yfinance_provider.py`

- Use batched downloads (`yf.download(" ".join(tickers), ...)`); per-ticker fallback on failure.
- Handle yfinance's MultiIndex column quirk.
- **Rate limit:** `time.sleep(0.2)` between fallback requests.
- **Column lowercase normalization.**
- Drop rows with `NaN` in any OHLCV column.

### 4.3 `synthetic_provider.py`

Generates deterministic OHLCV via geometric Brownian motion. Assign each ticker a regime (`bullish | bearish | choppy`) by hash so results are stable across runs. Bullish regime must have:
- Positive drift (~0.008)
- Volume spikes on up-days (so rel-vol signal has something to bite on)

### 4.4 `cache.py`

On-disk parquet cache keyed by `(ticker, interval, date)`. API:
```python
class BarCache:
    def __init__(self, path: Path): ...
    def get(self, ticker: str, interval: str) -> pd.DataFrame | None: ...
    def put(self, ticker: str, interval: str, df: pd.DataFrame) -> None: ...
```
Wrap any provider with `CachedProvider(wrapped, cache)` that checks cache first, only fetches what's missing.

**Verification:**
```bash
python -c "from scanner.data import get_provider; p = get_provider('synthetic'); df = p.get_bars('AAPL', '1d', 60); assert len(df) > 50; print(df.tail())"
```

---

## 5. Indicators (`scanner/indicators.py`)

Pure functions, no hidden state, all vectorized via pandas/numpy. Include:

| Function | Signature | Notes |
|---|---|---|
| `ema` | `(series, period) -> Series` | `ewm(span=period, adjust=False).mean()` |
| `sma` | `(series, period) -> Series` | |
| `rsi` | `(close, period=14) -> Series` | Wilder's smoothing. Clip to [0,100]. |
| `vwap` | `(df) -> Series` | Cumulative; reset daily if intraday (groupby session date). |
| `atr` | `(df, period=14) -> Series` | True range, Wilder's smoothing. |
| `relative_volume` | `(df, period=20) -> Series` | `volume / rolling_mean(volume)`. |
| `gap_pct` | `(df) -> Series` | `(open - prev_close) / prev_close * 100` |
| `pct_change_today` | `(df) -> Series` | `close.pct_change() * 100` |
| `distance_from_high` | `(df, period=20) -> Series` | Percent below rolling high. |
| `bollinger_bands` | `(close, period=20, n_std=2) -> tuple[Series,Series,Series]` | lower, mid, upper |

Expose a single `enrich(df) -> DataFrame` that appends all of the above as new columns (`ema9, ema20, ema50, rsi14, vwap, atr14, rel_volume, gap_pct, pct_change, dist_from_high_20, bb_lower, bb_mid, bb_upper`).

**Tests** (`tests/test_indicators.py`):
- RSI stays in [0, 100] on random walk input.
- EMA on constant series returns constant.
- Relative volume = 1.0 on constant-volume series.
- `enrich` adds all expected columns and doesn't drop rows.

**Verification:** `pytest tests/test_indicators.py -v` passes.

---

## 6. Signals (`scanner/signals.py`)

Each signal is a function that takes the enriched DataFrame and config, returns a strength in `[0, 1]` for the **latest bar**. Keep them pure and independently testable.

```python
def relative_volume_signal(df: pd.DataFrame, cfg: SignalThresholds) -> float: ...
def momentum_signal(df: pd.DataFrame, cfg: SignalThresholds) -> float: ...
def trend_alignment_signal(df: pd.DataFrame, cfg: SignalThresholds) -> tuple[float, dict]: ...
def rsi_position_signal(df: pd.DataFrame, cfg: SignalThresholds) -> float: ...
def breakout_proximity_signal(df: pd.DataFrame, cfg: SignalThresholds) -> float: ...
```

**Scoring curves (important — these are where the edge lives):**
- **Relative volume:** linear from threshold → 3.0× maps to 0 → 1. Clip.
- **Momentum:** linear from 0% → +5% maps to 0 → 1. Negatives clipped to 0.
- **Trend alignment:** 4 booleans (above VWAP, above EMA9, EMA9>EMA20, EMA20>EMA50). Score = count / 4. Return (score, breakdown dict).
- **RSI position:** triangular peak at midpoint of `[rsi_min, rsi_max]`. Linear decay to 0 at rsi_min and rsi_max. Above rsi_max, partial credit that decays to 0 at RSI=85 (overbought fade). Below rsi_min, 30% credit scaled by `rsi/rsi_min`.
- **Breakout proximity:** linear from 0% → 5% distance maps to 1 → 0. Clip.

**Hard filter gates:** `apply_hard_filters(df, cfg) -> bool` — returns False if the config says require-above-VWAP and price is below VWAP, etc. Called before scoring.

**Tests** (`tests/test_signals.py`):
- Each signal returns a float in [0, 1].
- Momentum returns 0 on a down day.
- Trend alignment returns 1.0 when all 4 conditions are satisfied (build a fixture).
- RSI position peaks at midpoint (test with RSI=60 when min=50, max=70 → ~1.0).

---

## 7. Scanner engine (`scanner/engine.py`)

The orchestrator. Pipeline:

```
for ticker in tickers:
    df = data[ticker]
    if len(df) < 25: skip
    enriched = enrich(df)
    if not passes_universe_filter(enriched, cfg): skip
    if not apply_hard_filters(enriched, cfg.thresholds): skip
    strengths = {name: signal(enriched, cfg.thresholds) for name, signal in SIGNALS.items()}
    score = weighted_sum(strengths, cfg.weights) * 100
    reasons = build_reasons(enriched, strengths)
    results.append(ScanResult(ticker, score, ..., strengths, reasons))
results.sort(key=score, reverse=True)
return results[:top_n]
```

`ScanResult` is a Pydantic model with:
- `ticker, score, price, pct_change, rel_volume, rsi`
- `above_vwap, above_ema9, ema_stacked, dist_from_20d_high_pct` (booleans/floats for display)
- `signals: dict[str, float]` — the 0–1 strengths
- `reasons: list[str]` — human-readable bullish factors
- `.to_dict()` that rounds numeric fields sensibly

**`build_reasons`** generates plain English:
- `"Relative volume 2.34x average"` if rel_vol ≥ threshold
- `"Up 2.63% on the day"` if pct ≥ 1%
- `"EMAs stacked bullishly (9>20>50)"` when true
- `"RSI 62 in bullish zone"` if in zone
- `"Within 0.35% of 20-day high"` when breakout proximity is high

**Tests** (`tests/test_engine.py`):
- Crafted bullish fixture (rising prices + volume spike) gets a score > 50.
- Crafted bearish fixture (falling prices) is filtered out by hard gates.
- Results are sorted descending by score.
- Top N honored.

---

## 8. Backtester (`scanner/backtest.py`)

Walk-forward replay. This is the single most important module — a scanner that isn't backtested is a random-number generator.

```python
def backtest(
    data: dict[str, pd.DataFrame],
    cfg: ScannerConfig,
    holding_bars: int = 3,
    target_pct: float = 2.0,
    min_history: int = 30,
) -> BacktestReport
```

Algorithm:
1. Collect union of all dates across tickers, sorted.
2. For each date `d` at index `i` where `min_history ≤ i ≤ len(dates) - holding_bars - 1`:
   - Build snapshot: each ticker's bars with `index <= d`.
   - Run `scan(snapshot, cfg)` → flagged tickers.
   - For each flag, measure forward window `[d+1, d+holding_bars]`:
     - `max_return_pct` = (window.high.max() - entry) / entry × 100
     - `min_return_pct` = (window.low.min() - entry) / entry × 100
     - `close_return_pct` = (window.close.last() - entry) / entry × 100
     - `hit_target` = max_return_pct ≥ target_pct
3. Aggregate into `BacktestReport`:
   - `n_signals, n_winners, hit_rate, avg_return_pct, median_return_pct`
   - `avg_max_return_pct, avg_max_drawdown_pct`
   - `profit_factor = sum(wins) / sum(|losses|)`
   - `expectancy_pct = avg_return_pct`
   - `trades: list[TradeOutcome]`
   - `equity_curve: list[dict]` — cumulative P&L over time for charting

**Look-ahead bias check:** write a test that confirms the backtester never uses data from `d+1` or later when computing the signal at date `d`.

**Tests:**
- Run on synthetic data, confirm `n_signals > 0` and report fields populated.
- Monotonicity: raising the thresholds reduces the signal count.
- Look-ahead bias: scan at date `d` produces the same ScanResult whether the dataset ends at `d` or `d+100` (run both ways, assert equal scores).

---

## 9. Tuner (`scanner/tuning.py`)

Grid search over key thresholds. Keep it dead simple — no fancy optimizer yet.

```python
def grid_search(
    data: dict[str, pd.DataFrame],
    param_grid: dict[str, list[float]],
    metric: str = "profit_factor",
    holding_bars: int = 3,
    target_pct: float = 2.0,
) -> list[dict]:
    """Returns list of {params, metrics} sorted by metric desc."""
```

Grid defaults:
- `min_relative_volume`: [1.2, 1.5, 2.0, 2.5]
- `rsi_min`: [45, 50, 55]
- `rsi_max`: [65, 70, 75]
- `breakout_proximity_pct`: [0.3, 0.5, 1.0]

**Train/test split:** split dates 70/30. Report in-sample and out-of-sample metrics. Warn loudly if OOS < 0.5 × IS (overfit alarm).

**Script:** `scripts/run_tuning.py` loads a universe, runs the grid, writes `output/tuning_results.csv`.

---

## 10. Alerts (`scanner/alerts.py`)

Pluggable notifier interface:

```python
class Notifier(Protocol):
    def send(self, results: list[ScanResult]) -> None: ...

class ConsoleNotifier: ...      # rich-formatted table
class SlackNotifier: ...         # POST to webhook_url
class DiscordNotifier: ...       # POST to webhook_url
```

Each alert must include: ticker, score, price, % change, relative volume, top 3 reasons, and a chart-deep-link (TradingView URL pattern: `https://www.tradingview.com/chart/?symbol={TICKER}`).

Read webhook URLs from env vars (`SLACK_WEBHOOK_URL`, `DISCORD_WEBHOOK_URL`). Skip notifier if env is unset.

---

## 11. CLI scripts (`scripts/`)

### `scripts/run_scan.py`

```
python scripts/run_scan.py [--provider yfinance|synthetic] [--tickers AAPL,MSFT]
                           [--interval 1d] [--lookback 90] [--notify console,slack]
```
Prints a rich table, writes `output/scan_results.json`.

### `scripts/run_backtest.py`

```
python scripts/run_backtest.py [--provider ...] [--holding-bars 3] [--target-pct 2.0]
```
Prints report, writes `output/backtest_report.json` + `output/equity_curve.csv`.

### `scripts/run_tuning.py`

```
python scripts/run_tuning.py [--provider ...] [--out output/tuning_results.csv]
```

### `run.sh`

Convenience launcher:
```bash
#!/usr/bin/env bash
set -e
case "$1" in
  scan)     python scripts/run_scan.py "${@:2}" ;;
  backtest) python scripts/run_backtest.py "${@:2}" ;;
  tune)     python scripts/run_tuning.py "${@:2}" ;;
  api)      uvicorn api.server:app --reload --port 8787 ;;
  dash)     cd dashboard && npm run dev ;;
  test)     pytest tests/ -v ;;
  *)        echo "Usage: ./run.sh {scan|backtest|tune|api|dash|test}"; exit 1 ;;
esac
```
`chmod +x run.sh`.

---

## 12. API server (`api/server.py`)

FastAPI app exposing these endpoints. All responses are JSON.

```
GET  /health                         → {"status": "ok"}
GET  /config                         → current ScannerConfig
POST /config                         → update config (JSON body)
POST /scan     {provider, tickers?}  → {results: [ScanResult...], ran_at}
POST /backtest {provider, tickers?, holding_bars, target_pct}
                                     → full BacktestReport JSON
GET  /universe                       → default ticker list
```

Enable CORS for `http://localhost:5173` (Vite dev server).
Cache scan results for 60 seconds (in-memory) so dashboard polling is cheap.

**Verification:**
```bash
./run.sh api &
curl -s http://localhost:8787/health   # → {"status":"ok"}
curl -s -X POST http://localhost:8787/scan -H 'content-type: application/json' \
     -d '{"provider":"synthetic"}' | jq '.results | length'
```

---

## 13. Dashboard (`dashboard/`)

**Stack:** Vite + React + TypeScript + Tailwind (or plain CSS modules — pick one and stay consistent) + Recharts.

### Routes / views

The dashboard is a single page with three sections stacked vertically:

1. **Header bar** — title ("Bullish Scanner"), last-refreshed timestamp, provider switcher (synthetic/yfinance), "Run Scan" button.
2. **Summary metric strip** — 4 metric cards:
   - # candidates scanned
   - # passing filters
   - Top score
   - Avg score of top 10
3. **Scan results table** (`ScanTable.tsx`) — sortable, one row per result:
   - Columns: Ticker • Score (with colored progress bar 0–100) • Price • %Δ • Rel Vol • RSI • Flags (EMA↑, VWAP↑ pills) • Reasons (collapsed; expandable)
   - Row click → expand `ScoreBreakdown.tsx` which is a horizontal bar chart of the 5 signal strengths.
4. **Backtest panel** — two charts side-by-side:
   - **TradeScatter.tsx**: scatter plot of `score` (x) vs `close_return_pct` (y), one dot per trade, colored green if winner / red if loser. Hover tooltip shows ticker, entry date, return.
   - **EquityCurve.tsx**: line chart of cumulative P&L over time from the equity curve.
   - Below: backtest summary table (hit rate, avg return, profit factor, expectancy).

### Data fetching (`api.ts`)

```typescript
export async function runScan(provider: string): Promise<ScanResponse> { ... }
export async function runBacktest(provider: string, params: BacktestParams): Promise<BacktestReport> { ... }
```

Point at `http://localhost:8787`. Use React Query or plain `useEffect` + `useState` — your call, but handle loading/error states visibly.

### Design guidance

- Dark-mode first; light-mode via `prefers-color-scheme`.
- Monospace font for numeric columns so they align.
- Don't use purple-gradient AI-slop aesthetics. Pick a clean editorial look — tight typography, generous whitespace, one accent color (e.g. green/amber for the score gauge), everything else neutral gray.
- Score bar: gray track, green fill when score ≥ 50, amber when 30–50, red when < 30.
- Numbers: always right-aligned, 2 decimal places, use `Intl.NumberFormat`.
- Negatives formatted as `-$5.20`, not `$-5.20`.

### Dashboard tests

Basic Vitest smoke tests: components render without throwing given mock data.

**Verification:**
```bash
cd dashboard
npm install
npm run build      # no errors
npm run dev        # opens on :5173, shows data from :8787
```

---

## 14. End-to-end acceptance

After building everything, this sequence must succeed on a fresh machine:

```bash
git clone <repo> && cd bullish_scanner
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Tests pass
./run.sh test
# expect: all green, coverage > 70% on scanner/

# 2. Synthetic scan works offline
./run.sh scan --provider synthetic
# expect: ranked table printed, output/scan_results.json written

# 3. Backtest produces real numbers
./run.sh backtest --provider synthetic
# expect: hit rate, profit factor, expectancy all populated

# 4. Tuner finds something
./run.sh tune --provider synthetic
# expect: output/tuning_results.csv with sorted rows

# 5. Live data works (requires internet)
./run.sh scan --provider yfinance --tickers AAPL,MSFT,GOOGL,NVDA,TSLA

# 6. Full stack up
./run.sh api &
./run.sh dash
# open localhost:5173 → dashboard loads, "Run Scan" populates table,
# backtest charts render with real data
```

---

## 15. Documentation

`README.md` must contain:
- 30-second pitch
- Architecture diagram (ASCII is fine)
- Quickstart (6 lines of shell)
- How to tune (pointer to `run.sh tune`)
- How to swap in a real data provider (pointer to `scanner/data/base.py`)
- **Honest disclaimers** — day trading is hard, most participants lose money, backtests on free EOD data are optimistic relative to live intraday execution, this is not financial advice.

---

## 16. Stretch goals (only after everything above works)

In priority order. Each one is optional.

1. **Intraday bars.** Add 5-minute interval support; update VWAP to reset per session via `groupby(df.index.date)`.
2. **News catalyst overlay.** Pull headlines from a free source (yfinance news, Finnhub free tier) and boost score if news landed in the last 60 minutes.
3. **Polygon provider.** Implement `polygon_provider.py` conforming to `MarketDataProvider`. Gate on `POLYGON_API_KEY` env var.
4. **Paper trading hook.** Add `scanner/paper_trader.py` that simulates fills on scan signals and writes trade logs.
5. **Screener persistence.** SQLite table of every scan result with a foreign key into outcomes, so hit-rate can be tracked live, not just in backtest.

---

## Definition of done

Check all of these before declaring the project complete:

- [ ] `./run.sh test` — all tests pass, coverage ≥ 70% on `scanner/`
- [ ] `./run.sh scan --provider synthetic` — writes valid JSON
- [ ] `./run.sh backtest --provider synthetic` — reports populated
- [ ] `./run.sh tune --provider synthetic` — writes tuning CSV
- [ ] `./run.sh scan --provider yfinance --tickers AAPL` — works online
- [ ] `./run.sh api` + `./run.sh dash` — dashboard renders, runs scan and backtest end-to-end
- [ ] `mypy --strict scanner/` — no errors
- [ ] `ruff check .` — no errors
- [ ] README updated with working quickstart
- [ ] Disclaimers present about day-trading risk
