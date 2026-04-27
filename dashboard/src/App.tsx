import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { applyPreset, listPresets, runBacktest, runScan } from './api'
import type { PresetSummary } from './api'
import type { BacktestReport, ScanResponse } from './types'
import { ScanTable } from './components/ScanTable'
import { BacktestSummary } from './components/BacktestSummary'
import { TradeScatter } from './components/TradeScatter'
import { EquityCurve } from './components/EquityCurve'
import { fmtNumber } from './format'

type Provider = 'synthetic' | 'yfinance' | 'ibkr'
type Interval = '1d' | '1h' | '15m' | '5m' | '1m'

// Intraday intervals want shorter lookback; daily wants longer.
const DEFAULT_LOOKBACK: Record<Interval, number> = {
  '1d': 90,
  '1h': 30,
  '15m': 10,
  '5m': 5,
  '1m': 2,
}

export default function App() {
  const [provider, setProvider] = useState<Provider>('synthetic')
  const [interval, setInterval] = useState<Interval>('1d')
  const [lookback, setLookback] = useState<number>(DEFAULT_LOOKBACK['1d'])
  const [scan, setScan] = useState<ScanResponse | null>(null)
  const [backtest, setBacktest] = useState<BacktestReport | null>(null)
  const [loadingScan, setLoadingScan] = useState(false)
  const [loadingBt, setLoadingBt] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [presets, setPresets] = useState<PresetSummary[]>([])
  const [preset, setPreset] = useState<string>('')
  const [autoRefreshSec, setAutoRefreshSec] = useState<number>(0) // 0 = off
  const [nextRefreshIn, setNextRefreshIn] = useState<number>(0)

  useEffect(() => {
    listPresets().then(setPresets).catch((e) => console.warn('presets failed', e))
  }, [])

  // Auto-refresh: trigger a scan every `autoRefreshSec`, plus a countdown tick
  // so the user can see when the next poll fires. 0 disables.
  useEffect(() => {
    if (autoRefreshSec <= 0) {
      setNextRefreshIn(0)
      return
    }
    setNextRefreshIn(autoRefreshSec)
    const tick = window.setInterval(() => {
      setNextRefreshIn((s) => (s <= 1 ? autoRefreshSec : s - 1))
    }, 1000)
    const scan = window.setInterval(() => {
      void handleScanRef.current?.()
    }, autoRefreshSec * 1000)
    return () => {
      window.clearInterval(tick)
      window.clearInterval(scan)
    }
  }, [autoRefreshSec])

  const handlePresetChange = useCallback(async (name: string) => {
    setPreset(name)
    if (!name) return
    setErr(null)
    try {
      const cfg = await applyPreset(name)
      const nextInterval = cfg.interval as Interval
      setInterval(nextInterval)
      setLookback(cfg.lookback_days)
    } catch (e) {
      setErr(String(e))
    }
  }, [])

  const changeInterval = (next: Interval) => {
    setInterval(next)
    setLookback(DEFAULT_LOOKBACK[next])
  }

  const handleScan = useCallback(async () => {
    setErr(null)
    setLoadingScan(true)
    try {
      const data = await runScan(provider, { interval, lookback_days: lookback })
      setScan(data)
    } catch (e) {
      setErr(String(e))
    } finally {
      setLoadingScan(false)
    }
  }, [provider, interval, lookback])

  // setInterval callbacks would capture a stale handleScan. A ref keeps the
  // always-current version accessible from the auto-refresh timer.
  const handleScanRef = useRef(handleScan)
  useEffect(() => {
    handleScanRef.current = handleScan
  }, [handleScan])

  const handleBacktest = useCallback(async () => {
    setErr(null)
    setLoadingBt(true)
    try {
      const data = await runBacktest(provider, { lookback_days: lookback })
      setBacktest(data)
    } catch (e) {
      setErr(String(e))
    } finally {
      setLoadingBt(false)
    }
  }, [provider, lookback])

  const topScore = useMemo(() => {
    if (!scan?.results?.length) return 0
    return Math.max(...scan.results.map((r) => r.score))
  }, [scan])

  const avgTop10 = useMemo(() => {
    if (!scan?.results?.length) return 0
    const top = scan.results.slice(0, 10)
    return top.reduce((s, r) => s + r.score, 0) / top.length
  }, [scan])

  return (
    <div className="mx-auto max-w-7xl px-4 py-6">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4 border-b border-neutral-200 pb-4 dark:border-neutral-800">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Bullish Scanner</h1>
          <p className="text-xs text-neutral-500">
            {scan ? `Last scan: ${new Date(scan.ran_at).toLocaleString()}` : 'Not yet run'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {presets.length > 0 && (
            <select
              value={preset}
              onChange={(e) => handlePresetChange(e.target.value)}
              className="rounded-md border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
              title="Apply a saved config preset"
            >
              <option value="">— Preset —</option>
              {presets.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name} ({p.interval}, {p.n_tickers} tkrs)
                </option>
              ))}
            </select>
          )}
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value as Provider)}
            className="rounded-md border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
            title="Data provider"
          >
            <option value="synthetic">Synthetic</option>
            <option value="yfinance">yfinance (live)</option>
            <option value="ibkr">IBKR (TWS)</option>
          </select>
          <select
            value={interval}
            onChange={(e) => changeInterval(e.target.value as Interval)}
            className="rounded-md border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
            title="Bar interval"
          >
            <option value="1d">1d</option>
            <option value="1h">1h</option>
            <option value="15m">15m</option>
            <option value="5m">5m</option>
            <option value="1m">1m</option>
          </select>
          <input
            type="number"
            value={lookback}
            onChange={(e) => setLookback(Math.max(1, Number(e.target.value) || 1))}
            className="w-16 rounded-md border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
            title="Lookback days"
            min={1}
          />
          <select
            value={autoRefreshSec}
            onChange={(e) => setAutoRefreshSec(Number(e.target.value))}
            className="rounded-md border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
            title="Auto-refresh interval (client-side; respects IB pacing)"
          >
            <option value={0}>Auto-refresh: off</option>
            <option value={60}>every 1 min</option>
            <option value={120}>every 2 min</option>
            <option value={300}>every 5 min</option>
            <option value={600}>every 10 min</option>
          </select>
          <button
            type="button"
            onClick={handleScan}
            disabled={loadingScan}
            className="rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-neutral-900 disabled:opacity-50"
          >
            {loadingScan ? 'Scanning…' : autoRefreshSec > 0 ? `Run Scan (${nextRefreshIn}s)` : 'Run Scan'}
          </button>
          <button
            type="button"
            onClick={handleBacktest}
            disabled={loadingBt}
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium disabled:opacity-50 dark:border-neutral-700"
          >
            {loadingBt ? 'Running…' : 'Run Backtest'}
          </button>
        </div>
      </header>

      {err && (
        <div className="mb-4 rounded-md border border-[var(--color-danger)] bg-red-50 px-3 py-2 text-sm text-[var(--color-danger)] dark:bg-red-950/40">
          {err}
        </div>
      )}

      <section className="mb-8">
        <MetricStrip
          scanned={scan?.n_candidates_scanned ?? 0}
          passing={scan?.n_results ?? 0}
          topScore={topScore}
          avgTop10={avgTop10}
        />
        <div className="mt-4 rounded-lg border border-neutral-200 dark:border-neutral-800">
          <ScanTable results={scan?.results ?? []} />
        </div>
      </section>

      <section className="mb-8">
        <h2 className="mb-3 text-base font-semibold">Backtest</h2>
        {backtest ? (
          <div className="space-y-4">
            <BacktestSummary report={backtest} />
            <div className="grid gap-4 lg:grid-cols-2">
              <Panel title="Score vs close return">
                <TradeScatter trades={backtest.trades} />
              </Panel>
              <Panel title="Equity curve">
                <EquityCurve points={backtest.equity_curve} />
              </Panel>
            </div>
          </div>
        ) : (
          <div className="rounded-md border border-dashed border-neutral-300 p-6 text-sm text-neutral-500 dark:border-neutral-700">
            Click "Run Backtest" to simulate walk-forward trades on the current provider.
          </div>
        )}
      </section>

      <footer className="border-t border-neutral-200 pt-4 text-xs text-neutral-500 dark:border-neutral-800">
        Not financial advice. Backtests on free EOD data are optimistic relative to live
        intraday execution.
      </footer>
    </div>
  )
}

function MetricStrip({
  scanned,
  passing,
  topScore,
  avgTop10,
}: {
  scanned: number
  passing: number
  topScore: number
  avgTop10: number
}) {
  const cards: Array<[string, string]> = [
    ['Candidates scanned', String(scanned)],
    ['Passing filters', String(passing)],
    ['Top score', fmtNumber(topScore)],
    ['Avg top-10 score', fmtNumber(avgTop10)],
  ]
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {cards.map(([k, v]) => (
        <div key={k} className="rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
          <div className="text-xs uppercase tracking-wide text-neutral-500">{k}</div>
          <div className="num mt-1 text-lg font-medium">{v}</div>
        </div>
      ))}
    </div>
  )
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
      <div className="mb-2 text-xs uppercase tracking-wide text-neutral-500">{title}</div>
      {children}
    </div>
  )
}
