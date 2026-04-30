import { useState } from 'react'
import type { ScanResult } from '../types'
import { fmtCurrency, fmtMultiplier, fmtNumber, fmtPct } from '../format'
import { ScoreBreakdown } from './ScoreBreakdown'
import { TradingViewChart } from './TradingViewChart'

interface Props {
  results: ScanResult[]
}

type SortKey = 'score' | 'price' | 'pct_change' | 'rel_volume' | 'rsi'

function scoreClasses(score: number): string {
  if (score >= 50) return 'bg-[var(--color-accent)]'
  if (score >= 30) return 'bg-[var(--color-warn)]'
  return 'bg-[var(--color-danger)]'
}

export function ScanTable({ results }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('score')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [expanded, setExpanded] = useState<string | null>(null)

  const sorted = [...results].sort((a, b) => {
    const av = a[sortKey]
    const bv = b[sortKey]
    return sortDir === 'desc' ? Number(bv) - Number(av) : Number(av) - Number(bv)
  })

  const toggleSort = (k: SortKey) => {
    if (k === sortKey) setSortDir(sortDir === 'desc' ? 'asc' : 'desc')
    else {
      setSortKey(k)
      setSortDir('desc')
    }
  }

  const header = (k: SortKey, label: string, extra = '') => (
    <th
      onClick={() => toggleSort(k)}
      className={`cursor-pointer select-none px-3 py-2 text-xs uppercase tracking-wide text-neutral-500 hover:text-neutral-900 dark:hover:text-neutral-50 ${extra}`}
    >
      {label}
      {sortKey === k ? (sortDir === 'desc' ? ' ▼' : ' ▲') : ''}
    </th>
  )

  if (!results.length) {
    return (
      <div className="py-10 text-center text-sm text-neutral-500">
        No results. Click "Run Scan" to fetch a fresh batch.
      </div>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-neutral-200 dark:border-neutral-800">
            <th className="px-3 py-2 text-left text-xs uppercase tracking-wide text-neutral-500">
              Ticker
            </th>
            {header('score', 'Score', 'text-right')}
            {header('price', 'Price', 'text-right')}
            {header('pct_change', '% Δ', 'text-right')}
            {header('rel_volume', 'Rel Vol', 'text-right')}
            {header('rsi', 'RSI', 'text-right')}
            <th className="px-3 py-2 text-left text-xs uppercase tracking-wide text-neutral-500">
              Flags
            </th>
            <th className="px-3 py-2 text-left text-xs uppercase tracking-wide text-neutral-500">
              Reasons
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => {
            const isOpen = expanded === r.ticker
            return (
              <>
                <tr
                  key={r.ticker}
                  onClick={() => setExpanded(isOpen ? null : r.ticker)}
                  className="cursor-pointer border-b border-neutral-200 transition-colors hover:bg-neutral-100 dark:border-neutral-800 dark:hover:bg-neutral-900"
                >
                  <td className="px-3 py-2 font-semibold">{r.ticker}</td>
                  <td className="num px-3 py-2">
                    <div className="flex items-center justify-end gap-2">
                      <div className="h-2 w-20 overflow-hidden rounded bg-neutral-200 dark:bg-neutral-800">
                        <div
                          className={`h-full ${scoreClasses(r.score)}`}
                          style={{ width: `${Math.min(100, Math.max(0, r.score))}%` }}
                        />
                      </div>
                      <span>{fmtNumber(r.score)}</span>
                    </div>
                  </td>
                  <td className="num px-3 py-2">{fmtCurrency(r.price)}</td>
                  <td
                    className={`num px-3 py-2 ${
                      r.pct_change >= 0 ? 'text-[var(--color-accent-dim)]' : 'text-[var(--color-danger)]'
                    }`}
                  >
                    {fmtPct(r.pct_change, true)}
                  </td>
                  <td className="num px-3 py-2">{fmtMultiplier(r.rel_volume)}</td>
                  <td className="num px-3 py-2">{fmtNumber(r.rsi)}</td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1 text-xs">
                      {r.above_vwap && <Pill label="VWAP↑" />}
                      {r.above_ema9 && <Pill label="EMA9↑" />}
                      {r.ema_stacked && <Pill label="Stacked" />}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-xs text-neutral-600 dark:text-neutral-400">
                    {r.reasons.slice(0, 2).join('; ') || '—'}
                  </td>
                </tr>
                {isOpen && (
                  <tr className="border-b border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-900/40">
                    <td colSpan={8} className="px-3 py-4">
                      <div className="space-y-4">
                        <div className="grid gap-4 lg:grid-cols-2">
                          <div>
                            <div className="mb-2 text-xs uppercase tracking-wide text-neutral-500">
                              Signal strengths
                            </div>
                            <ScoreBreakdown signals={r.signals} />
                          </div>
                          <div>
                            <div className="mb-2 text-xs uppercase tracking-wide text-neutral-500">
                              Reasons
                            </div>
                            <ul className="list-disc pl-5 text-sm">
                              {r.reasons.length === 0 && (
                                <li className="text-neutral-500">No qualitative factors</li>
                              )}
                              {r.reasons.map((reason, i) => (
                                <li key={i}>{reason}</li>
                              ))}
                            </ul>
                            <a
                              className="mt-3 inline-block text-xs text-[var(--color-accent-dim)] hover:underline"
                              href={`https://www.tradingview.com/chart/?symbol=${r.ticker}`}
                              target="_blank"
                              rel="noreferrer"
                            >
                              Open {r.ticker} on TradingView →
                            </a>
                          </div>
                        </div>
                        <div>
                          <div className="mb-2 text-xs uppercase tracking-wide text-neutral-500">
                            Chart — EMA(9) · RSI(9)
                          </div>
                          <TradingViewChart ticker={r.ticker} />
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function Pill({ label }: { label: string }) {
  return (
    <span className="rounded-full bg-neutral-200 px-2 py-0.5 text-[10px] font-medium uppercase text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300">
      {label}
    </span>
  )
}
