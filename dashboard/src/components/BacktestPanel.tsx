import { useCallback, useState } from 'react'
import { runBacktest } from '../api'
import type { BacktestReport } from '../types'
import { fmtNumber, fmtPct } from '../format'

interface Props {
  provider: string
  lookback: number
  onError: (msg: string | null) => void
}

export function BacktestPanel({ provider, lookback, onError }: Props) {
  const [holdingBars, setHoldingBars] = useState(3)
  const [targetPct, setTargetPct] = useState(2.0)
  const [report, setReport] = useState<BacktestReport | null>(null)
  const [loading, setLoading] = useState(false)

  const run = useCallback(async () => {
    onError(null)
    setLoading(true)
    try {
      const data = await runBacktest(provider, {
        lookback_days: lookback,
        holding_bars: holdingBars,
        target_pct: targetPct,
      })
      setReport(data)
    } catch (e) {
      onError(String(e))
    } finally {
      setLoading(false)
    }
  }, [provider, lookback, holdingBars, targetPct, onError])

  return (
    <div className="flex h-full flex-col gap-2 overflow-y-auto">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={run}
          disabled={loading}
          className="flex-1 rounded bg-[var(--color-accent)] px-3 py-1.5 text-xs font-semibold text-neutral-950 disabled:opacity-50"
        >
          {loading ? 'Running…' : 'Run Backtest'}
        </button>
      </div>

      <div className="flex items-center gap-2 text-xs">
        <label className="flex items-center gap-1">
          <span className="text-neutral-500">Hold</span>
          <input
            type="number"
            min={1}
            max={50}
            value={holdingBars}
            onChange={(e) => setHoldingBars(Math.max(1, Number(e.target.value) || 1))}
            className="w-12 rounded border border-neutral-700 bg-neutral-900 px-1.5 py-0.5"
          />
          <span className="text-neutral-500">bars</span>
        </label>
        <label className="flex items-center gap-1">
          <span className="text-neutral-500">Target</span>
          <input
            type="number"
            min={0.1}
            step={0.1}
            value={targetPct}
            onChange={(e) => setTargetPct(Math.max(0.1, Number(e.target.value) || 0.1))}
            className="w-14 rounded border border-neutral-700 bg-neutral-900 px-1.5 py-0.5"
          />
          <span className="text-neutral-500">%</span>
        </label>
      </div>

      {!report && !loading && (
        <p className="text-xs text-neutral-500">
          Walk-forward replay over the lookback window. No look-ahead — at each
          historical date, the scanner only sees prior bars.
        </p>
      )}

      {report && (
        <div className="space-y-2">
          <Metrics report={report} />
          <TradesList report={report} />
        </div>
      )}
    </div>
  )
}

function Metrics({ report }: { report: BacktestReport }) {
  const cells: Array<[string, string]> = [
    ['Signals', String(report.n_signals)],
    ['Hit rate', fmtPct(report.hit_rate * 100)],
    ['Avg ret', fmtPct(report.avg_return_pct, true)],
    ['PF', fmtNumber(report.profit_factor)],
    ['Avg max', fmtPct(report.avg_max_return_pct, true)],
    ['Avg DD', fmtPct(report.avg_max_drawdown_pct, true)],
  ]
  return (
    <div className="grid grid-cols-2 gap-1 text-xs">
      {cells.map(([k, v]) => (
        <div
          key={k}
          className="flex items-center justify-between rounded border border-neutral-800 bg-neutral-900/40 px-1.5 py-1"
        >
          <span className="text-[10px] uppercase tracking-wide text-neutral-500">{k}</span>
          <span className="num">{v}</span>
        </div>
      ))}
    </div>
  )
}

function TradesList({ report }: { report: BacktestReport }) {
  const trades = report.trades.slice(0, 30)
  if (trades.length === 0) {
    return null
  }
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-neutral-500">
        Recent trades ({trades.length} of {report.trades.length})
      </div>
      <ul className="mt-1 max-h-64 divide-y divide-neutral-900 overflow-y-auto">
        {trades.map((t, i) => (
          <li key={i} className="flex items-center justify-between px-1 py-0.5 text-xs">
            <span className="flex items-center gap-1.5">
              <span className="font-semibold">{t.ticker}</span>
              <span className="text-[10px] text-neutral-500">
                {t.entry_date.slice(0, 10)}
              </span>
            </span>
            <span
              className={`num ${
                t.close_return_pct >= 0
                  ? 'text-[var(--color-accent-dim)]'
                  : 'text-[var(--color-danger)]'
              }`}
            >
              {fmtPct(t.close_return_pct, true)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
