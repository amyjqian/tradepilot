# Build Plan — Bullish Scanner (full end-to-end)

## Context

The working directory `/Users/qianj/PycharmProjects/bullish_scanner` is a greenfield project. It currently contains only `BUILD.md` (a 16-section build spec) and the default PyCharm `main.py`. A `.venv` (Python 3.11.9) exists but is empty (pip/setuptools only). Node 24 / npm 11 are available. The directory is not a git repo.

BUILD.md prescribes a full-stack product: a Python scanner package (`scanner/`) + FastAPI server (`api/`) + React/Vite dashboard (`dashboard/`) + tests + CLI scripts. The user has confirmed scope covers **§1–15 end-to-end**, with **Tailwind CSS** for the dashboard and a **`git init` + `.gitignore`** at the start.

The intended outcome is a runnable local project where `./run.sh test|scan|backtest|tune|api|dash` all work, tests pass with ≥70% coverage on `scanner/`, `mypy --strict scanner/` is clean, `ruff check .` is clean, and the dashboard at `:5173` talks to the API at `:8787`.

`main.py` is the PyCharm default stub and can be deleted — the entry points are the scripts under `scripts/` and the two `run.sh` targets for API and dashboard.

## Approach

Build strictly in the order BUILD.md prescribes. Each milestone ends with the spec's verification step; do not start the next milestone until verification passes. If a verification step fails, stop and fix before continuing — the spec is explicit about this.

### Milestone 0 — Bootstrap
- `git init`, write `.gitignore` (Python: `__pycache__/`, `.venv/`, `*.pyc`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `dist/`, `build/`, `*.egg-info/`; Node: `node_modules/`, `dashboard/dist/`; project: `output/`, `data_cache/`; editor: `.idea/`, `.vscode/`).
- Delete `main.py` (PyCharm stub, not part of the deliverable).
- Create the full directory tree from §1 with empty `__init__.py` files so later sections just fill them in.
- **Verify:** `tree -L 2` matches §1.

### Milestone 1 — Dependencies (§2)
- Write `requirements.txt` exactly as specified.
- Write `pyproject.toml` configuring `ruff` (line length 100) and `mypy` (strict on `scanner/`).
- Install into `.venv`.
- **Verify:** `.venv/bin/pip install -r requirements.txt` succeeds.

### Milestone 2 — Config (§3) — `scanner/config.py`
- Pydantic `BaseModel`s: `UniverseConfig`, `SignalThresholds`, `ScoringWeights` (with `@model_validator` asserting weights sum to 1.0 within float tolerance), `ScannerConfig`.
- `ScannerConfig.default_tickers`: curate ~50 liquid US large/mid-caps across sectors (mega-cap tech: AAPL, MSFT, GOOGL, AMZN, META, NVDA; semis: AMD, AVGO, TSM, MU, QCOM, INTC; financials: JPM, BAC, GS, MS, V, MA; consumer: WMT, COST, HD, NKE, MCD, SBUX; healthcare: UNH, JNJ, LLY, PFE, ABBV, TMO; industrials: CAT, BA, DE, GE, HON; energy: XOM, CVX, COP; high-beta: TSLA, NFLX, CRM, COIN, PLTR, SNOW, SHOP, UBER, ABNB, AMD). Aim for exactly 50, sector-balanced.
- `from_json` / `to_json` helpers for tuner round-tripping.
- **Verify:** `python -c "from scanner.config import ScannerConfig; print(ScannerConfig().model_dump_json(indent=2))"`.

### Milestone 3 — Data layer (§4) — `scanner/data/`
- `base.py`: `MarketDataProvider` ABC with `get_bars` / `get_bars_batch`; `validate_bars(df)` helper enforcing lowercase `[open, high, low, close, volume]`, tz-aware ascending `DatetimeIndex`.
- `yfinance_provider.py`: batched `yf.download`, per-ticker fallback with `time.sleep(0.2)`, handle MultiIndex column quirk, lowercase normalize, drop `NaN` rows. Log-and-reraise on unexpected errors; log-and-skip-with-warning on per-ticker failure.
- `synthetic_provider.py`: deterministic GBM; regime assignment via `hash(ticker)` → bullish/bearish/choppy. Bullish regime: drift ≈ 0.008, volume spike on up-days.
- `cache.py`: `BarCache` (parquet per `(ticker, interval)`); `CachedProvider` wrapper that fetches only missing date ranges.
- `scanner/data/__init__.py`: expose `get_provider(name: str) -> MarketDataProvider`.
- **Verify:** `python -c "from scanner.data import get_provider; p = get_provider('synthetic'); df = p.get_bars('AAPL','1d',60); assert len(df)>50; print(df.tail())"`.

### Milestone 4 — Indicators (§5) — `scanner/indicators.py` + `tests/test_indicators.py`
- Pure, vectorized functions per the spec table: `ema`, `sma`, `rsi` (Wilder's, clipped [0,100]), `vwap` (reset daily via `groupby(session_date)` if intraday), `atr` (Wilder's), `relative_volume`, `gap_pct`, `pct_change_today`, `distance_from_high`, `bollinger_bands`.
- `enrich(df) -> DataFrame` appends all spec-named columns without dropping rows.
- Tests: RSI range invariant on random walk; EMA of constant is constant; rel_vol = 1.0 on constant volume; `enrich` column completeness.
- **Verify:** `pytest tests/test_indicators.py -v`.

### Milestone 5 — Signals (§6) — `scanner/signals.py` + `tests/test_signals.py`
- Five signal functions, each returning `float ∈ [0,1]` on the latest bar. `trend_alignment_signal` returns `(float, dict)`.
- Scoring curves exactly as specified — the RSI triangular-with-decay curve and the breakout-proximity linear-from-0%-to-5% mapping are where the edge lives, code them carefully.
- `apply_hard_filters(df, cfg)` gates on `require_above_vwap`, `require_above_ema9`, `require_ema9_above_ema20`, gap bounds.
- Tests per spec: range invariants, momentum=0 on down day, trend alignment = 1.0 when all four bools true (build fixture), RSI peak at midpoint.

### Milestone 6 — Engine (§7) — `scanner/engine.py` + `tests/test_engine.py`
- `ScanResult` Pydantic model with `.to_dict()` that rounds numerics.
- `SIGNALS` registry mapping name → callable.
- `scan(data, cfg)` pipeline exactly per spec: skip-if-short-history, universe filter, hard filters, weighted sum × 100, `build_reasons`, sort desc, top-N.
- `build_reasons` produces the five plain-English strings from the spec (rel vol, pct change, stacked EMAs, RSI zone, breakout proximity).
- Tests: bullish fixture scores >50; bearish fixture filtered; descending order; `top_n` honored.

### Milestone 7 — Backtester (§8) — `scanner/backtest.py` + `tests/test_backtest.py`
- Walk-forward loop over union of dates; snapshot-up-to-`d`; forward window `[d+1, d+holding_bars]`; compute max/min/close returns and `hit_target`.
- `BacktestReport` Pydantic: aggregates + `trades: list[TradeOutcome]` + `equity_curve` time series for frontend charts.
- **Critical test:** look-ahead bias check — scan at `d` on `data[:d]` must produce identical `ScanResult.score` as scan at `d` on `data[:d+100]`. Spec calls this out explicitly; if this test fails the entire backtester is untrustworthy.
- Additional tests: synthetic data produces `n_signals > 0`; raising thresholds monotonically reduces signal count.

### Milestone 8 — Tuner (§9) — `scanner/tuning.py` + `scripts/run_tuning.py`
- `grid_search` over the four spec parameters with defaults given; 70/30 date-based train/test split; warn loudly when OOS metric < 0.5 × IS.
- Script writes `output/tuning_results.csv` sorted by metric desc.

### Milestone 9 — Alerts (§10) — `scanner/alerts.py`
- `Notifier` Protocol + `ConsoleNotifier` (rich table), `SlackNotifier`, `DiscordNotifier`.
- Env-var gating on `SLACK_WEBHOOK_URL` / `DISCORD_WEBHOOK_URL`; skip notifier when unset.
- Per-alert payload: ticker, score, price, %Δ, rel_vol, top 3 reasons, TradingView deep-link `https://www.tradingview.com/chart/?symbol={TICKER}`.

### Milestone 10 — CLI + run.sh (§11)
- `scripts/run_scan.py`, `scripts/run_backtest.py`, `scripts/run_tuning.py` with the documented flag surfaces. Use `rich` for tables; write outputs to `output/`.
- `run.sh` exactly as specified; `chmod +x`.
- **Verify:** `./run.sh scan --provider synthetic` writes `output/scan_results.json`; `./run.sh backtest --provider synthetic` writes report + equity curve CSV.

### Milestone 11 — API (§12) — `api/server.py`
- FastAPI app with the seven endpoints. 60-second in-memory cache on `/scan`. CORS for `http://localhost:5173`.
- **Verify:** `./run.sh api &` + curl `/health` + `POST /scan` returns non-empty `results`.

### Milestone 12 — Dashboard (§13) — `dashboard/`
- Vite + React + TypeScript + **Tailwind** + Recharts. `npm create vite@latest . -- --template react-ts` inside `dashboard/`, then add Tailwind via the standard `@tailwindcss/postcss` or `tailwindcss init -p` path depending on current Tailwind version at install time.
- Components: `App.tsx` (three stacked sections), `ScanTable.tsx`, `ScoreBreakdown.tsx` (horizontal bar chart of the 5 signal strengths, revealed on row expand), `BacktestSummary.tsx`, `TradeScatter.tsx` (score vs close_return_pct, green/red by winner), `EquityCurve.tsx`.
- `api.ts`: typed `runScan` / `runBacktest` against `http://localhost:8787`.
- Design constraints from spec: dark-mode first via `prefers-color-scheme`; monospace numerics, right-aligned, 2 decimals via `Intl.NumberFormat`; negatives `-$5.20` not `$-5.20`; score bar color thresholds 50/30; no purple-gradient aesthetics, one accent color.
- Vitest smoke tests rendering components with mock data.
- **Verify:** `npm install && npm run build` clean; `npm run dev` serves on `:5173` and renders scan+backtest data.

### Milestone 13 — Quality gates (§14 + Definition of done)
- `./run.sh test` all green, coverage ≥ 70% on `scanner/`.
- `mypy --strict scanner/` clean.
- `ruff check .` clean.
- `./run.sh scan --provider yfinance --tickers AAPL,MSFT,GOOGL,NVDA,TSLA` works against live data.
- End-to-end smoke: API + dashboard both running, "Run Scan" populates the table, backtest charts render.

### Milestone 14 — Docs (§15) — `README.md`
- 30-second pitch, ASCII architecture diagram, 6-line quickstart, pointers to tuner + `data/base.py` for provider swap, honest day-trading risk disclaimers.

## Critical files (primary targets for edits)

```
scanner/config.py          scanner/engine.py           scanner/backtest.py
scanner/indicators.py      scanner/signals.py          scanner/tuning.py
scanner/alerts.py          scanner/data/base.py        scanner/data/yfinance_provider.py
scanner/data/synthetic_provider.py                     scanner/data/cache.py
scanner/data/__init__.py
api/server.py
dashboard/src/App.tsx      dashboard/src/api.ts        dashboard/src/components/*.tsx
dashboard/tailwind.config.ts                           dashboard/vite.config.ts
tests/test_indicators.py   tests/test_signals.py       tests/test_engine.py
tests/test_backtest.py     tests/test_data.py
scripts/run_scan.py        scripts/run_backtest.py     scripts/run_tuning.py
run.sh                     pyproject.toml              requirements.txt
README.md                  .gitignore
```

## Reusable utilities

This is a greenfield build — no existing functions to reuse. External libraries the spec mandates: `pandas` (indicators, DataFrames), `numpy` (vectorization), `yfinance` (live data), `pydantic` v2 (all config + result models), `fastapi` + `uvicorn`, `pyarrow` (parquet cache), `httpx` (webhook POSTs), `rich` (CLI tables), `pytest` / `mypy` / `ruff`.

## Non-obvious spec requirements to honor

- `from __future__ import annotations` at the top of every `scanner/` module; `mypy --strict` must pass on the package.
- Never silently catch exceptions — log + reraise, or log + skip with a visible warning.
- Scanner logic must be deterministic and idempotent — no `datetime.now()`, no `random` without a seed, inside scoring.
- Backtester must be provably free of look-ahead bias — the cross-dataset equality test is mandatory.
- Dashboard styling must avoid "purple-gradient AI-slop aesthetics" — tight editorial typography, one accent color, gray neutrals.

## Verification (end-to-end acceptance, per spec §14)

Run in order after the full build. Each step gates the next.

```bash
# 1 — tests + coverage
./run.sh test                                    # all green, coverage ≥ 70% on scanner/

# 2 — offline scan
./run.sh scan --provider synthetic               # ranked table + output/scan_results.json

# 3 — backtest
./run.sh backtest --provider synthetic           # hit rate, profit factor, expectancy populated

# 4 — tuner
./run.sh tune --provider synthetic               # output/tuning_results.csv sorted

# 5 — live data (requires internet)
./run.sh scan --provider yfinance --tickers AAPL,MSFT,GOOGL,NVDA,TSLA

# 6 — full stack
./run.sh api &
./run.sh dash                                    # :5173 loads, Run Scan populates, charts render

# 7 — quality gates
mypy --strict scanner/
ruff check .
```

## Risks / watch-outs

- **yfinance flakiness.** The free endpoint rate-limits and occasionally returns empty frames; the per-ticker fallback + `NaN`-drop is spec-required, not optional. Do all dev/testing against the synthetic provider; only touch yfinance at the §14 live-data verification step.
- **Look-ahead bias in backtester.** Easy to introduce accidentally via vectorized indicators that peek at future rows. The mandatory cross-dataset equality test is the defense — do not skip or weaken it.
- **Tailwind version drift.** Tailwind v4 changed the PostCSS integration; follow whatever the currently installed version's docs say rather than copy-pasting v3 config.
- **Coverage target.** 70% on `scanner/` includes the backtester — make sure backtest tests actually exercise the walk-forward loop, not just helpers.
- **Scope.** This is a large build. Expect many files and a long session; each milestone's verification step is the natural checkpoint to pause and recover if anything regresses.
