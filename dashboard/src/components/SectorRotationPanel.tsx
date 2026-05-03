import { useCallback, useEffect, useMemo, useState } from 'react'
import { runSectorRotation } from '../api'
import type { ScanResult, SectorRotationResponse } from '../types'
import { fmtNumber, fmtPct } from '../format'
import { useResultFilters } from '../useResultFilters'
import { ResultFilters } from './ResultFilters'

interface Props {
  provider: string
  interval: string
  lookback: number
  selected: string | null
  onSelect: (r: ScanResult) => void
  onError: (msg: string | null) => void
}

export function SectorRotationPanel({
  provider,
  interval,
  lookback,
  selected,
  onSelect,
  onError,
}: Props) {
  const [data, setData] = useState<SectorRotationResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [topN, setTopN] = useState<number>(2)
  /** ETF symbol the user clicked to drill into; `null` = pooled view of all top-N. */
  const [activeSector, setActiveSector] = useState<string | null>(null)
  const filters = useResultFilters()

  const run = useCallback(async () => {
    onError(null)
    setLoading(true)
    try {
      const res = await runSectorRotation(provider, {
        interval,
        lookback_days: lookback,
        top_n: topN,
      })
      setData(res)
      setActiveSector(null)
    } catch (e) {
      onError(String(e))
    } finally {
      setLoading(false)
    }
  }, [provider, interval, lookback, topN, onError])

  // If a re-rank changes the top-N sectors, drop a stale activeSector
  // selection that no longer points at a top-N row.
  useEffect(() => {
    if (data && activeSector && !data.top_etfs.includes(activeSector)) {
      setActiveSector(null)
    }
  }, [data, activeSector])

  const topSet = new Set(data?.top_etfs ?? [])

  /** Filter the pooled results to the user-selected sector, or show
   * everything when nothing is selected. Then apply the trend filter
   * pills on top — the two filters compose: sector-narrow first, then
   * trend-narrow. */
  const sectorResults = useMemo(() => {
    if (!data) return []
    if (!activeSector) return data.results
    const allowed = new Set(data.top_constituents_by_sector[activeSector] ?? [])
    return data.results.filter((r) => allowed.has(r.ticker))
  }, [data, activeSector])

  const visibleResults = useMemo(
    () => filters.apply(sectorResults),
    [sectorResults, filters],
  )

  const headerLabel =
    data && data.top_etfs.length > 0
      ? activeSector ?? data.top_etfs.join(' + ')
      : '—'

  const toggleSector = (etf: string) => {
    setActiveSector((prev) => (prev === etf ? null : etf))
  }

  return (
    <div className="flex h-full flex-col gap-2 overflow-hidden">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={run}
          disabled={loading}
          className="flex-1 rounded bg-[var(--color-accent)] px-3 py-1.5 text-xs font-semibold text-neutral-950 disabled:opacity-50"
        >
          {loading ? 'Ranking…' : `Rank Sectors (top ${topN})`}
        </button>
        <label
          className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-neutral-500"
          title="How many top-ranked sectors to pool for the constituent scan"
        >
          N
          <select
            value={topN}
            onChange={(e) => setTopN(Number(e.target.value))}
            className="rounded border border-neutral-700 bg-neutral-900 px-1 py-0.5 text-xs text-neutral-100"
          >
            <option value={1}>1</option>
            <option value={2}>2</option>
            <option value={3}>3</option>
          </select>
        </label>
      </div>

      {!data && !loading && (
        <p className="text-xs text-neutral-500">
          Ranks the 11 SPDR sector ETFs by 5-bar relative strength vs SPY plus a
          trend filter, then scores stocks within the top {topN} sector
          {topN === 1 ? '' : 's'} pooled.
        </p>
      )}

      {data && (
        <>
          <div className="border-b border-neutral-800 pb-1">
            <div className="text-[10px] uppercase tracking-wide text-neutral-500">
              Sectors{' '}
              <span className="ml-1 text-neutral-600 normal-case tracking-normal">
                (click a highlighted row to filter results)
              </span>
            </div>
            <ul className="mt-1 space-y-0.5">
              {data.ranked.map((s, idx) => {
                const isTop = topSet.has(s.etf)
                const isActive = activeSector === s.etf
                const w = Math.min(100, Math.max(0, Math.abs(s.score) * 8))
                const positive = s.score >= 0
                const baseClass =
                  'flex items-center justify-between rounded px-1 py-0.5 text-xs'
                const stateClass = isActive
                  ? 'bg-[var(--color-accent)]/30 ring-1 ring-[var(--color-accent)]'
                  : isTop
                    ? 'bg-[var(--color-accent)]/15 hover:bg-[var(--color-accent)]/25 cursor-pointer'
                    : ''
                const inner = (
                  <>
                    <span className="flex items-center gap-1.5">
                      <span className="num w-5 text-[10px] text-neutral-500">
                        {idx + 1}
                      </span>
                      <span className="font-semibold">{s.etf}</span>
                      <span className="text-[10px] text-neutral-500">{s.name}</span>
                    </span>
                    <span className="flex items-center gap-1.5">
                      <span
                        className="h-1.5 w-12 overflow-hidden rounded bg-neutral-800"
                        title={
                          `Rotation score ${fmtPct(s.score, true)} ` +
                          `(${fmtPct(s.excess_return_5_vs_spy, true)} vs SPY` +
                          `${s.above_ema20 ? ' + 0.5 trend bonus' : ''})`
                        }
                      >
                        <span
                          className={`block h-full ${
                            positive
                              ? 'bg-[var(--color-accent)]'
                              : 'bg-[var(--color-danger)]'
                          }`}
                          style={{ width: `${w}%` }}
                        />
                      </span>
                      <span
                        className={`num w-12 text-right text-[11px] ${
                          s.pct_change_5 >= 0
                            ? 'text-[var(--color-accent-dim)]'
                            : 'text-[var(--color-danger)]'
                        }`}
                        title={`5-bar % change of ${s.etf}`}
                      >
                        {fmtPct(s.pct_change_5, true)}
                      </span>
                    </span>
                  </>
                )
                return (
                  <li key={s.etf}>
                    {isTop ? (
                      <button
                        type="button"
                        onClick={() => toggleSector(s.etf)}
                        className={`${baseClass} ${stateClass} w-full text-left`}
                        title={
                          isActive
                            ? `Clear filter (back to top ${data.top_etfs.length} pooled)`
                            : `Show only ${s.etf} stocks in the result list`
                        }
                      >
                        {inner}
                      </button>
                    ) : (
                      <div className={baseClass}>{inner}</div>
                    )}
                  </li>
                )
              })}
            </ul>
          </div>

          <div className="flex flex-1 flex-col overflow-hidden">
            <div className="flex flex-wrap items-center justify-between gap-1">
              <div className="text-[10px] uppercase tracking-wide text-neutral-500">
                Top stocks in {headerLabel} (
                {filters.hasActive
                  ? `${visibleResults.length} of ${sectorResults.length}`
                  : visibleResults.length}
                )
              </div>
              {activeSector && (
                <button
                  type="button"
                  onClick={() => setActiveSector(null)}
                  className="rounded border border-neutral-700 px-1.5 py-0 text-[10px] text-neutral-400 hover:bg-neutral-800"
                  title="Show pooled top-N results"
                >
                  All ({data.results.length})
                </button>
              )}
            </div>
            <ResultFilters
              state={filters.state}
              hasActive={filters.hasActive}
              toggleTrend={filters.toggleTrend}
              toggleGreen={filters.toggleGreen}
              toggleNearHigh={filters.toggleNearHigh}
              setMinScore={filters.setMinScore}
              setRsiMin={filters.setRsiMin}
              setRsiMax={filters.setRsiMax}
              onClear={filters.clear}
            />
            <div className="flex-1 overflow-y-auto">
              {visibleResults.length === 0 ? (
                <p className="py-2 text-xs text-neutral-500">
                  {filters.hasActive && sectorResults.length > 0
                    ? `All ${sectorResults.length} hits filtered out — clear a pill to see them.`
                    : 'No constituents passed filters.'}
                </p>
              ) : (
                <ul className="divide-y divide-neutral-900">
                  {visibleResults.map((r) => (
                    <ScannerRow
                      key={r.ticker}
                      result={r}
                      isSelected={selected === r.ticker}
                      onClick={() => onSelect(r)}
                    />
                  ))}
                </ul>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function ScannerRow({
  result,
  isSelected,
  onClick,
}: {
  result: ScanResult
  isSelected: boolean
  onClick: () => void
}) {
  const r = result
  const scoreColor =
    r.score >= 50
      ? 'bg-[var(--color-accent)]'
      : r.score >= 30
        ? 'bg-[var(--color-warn)]'
        : 'bg-[var(--color-danger)]'

  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        className={`flex w-full items-center justify-between gap-2 px-1 py-1 text-left text-xs hover:bg-neutral-900 ${
          isSelected ? 'bg-[var(--color-accent)]/15' : ''
        }`}
      >
        <span className="flex items-center gap-2">
          <span className="font-semibold">{r.ticker}</span>
          <span
            className={`num text-[11px] ${
              r.pct_change >= 0
                ? 'text-[var(--color-accent-dim)]'
                : 'text-[var(--color-danger)]'
            }`}
          >
            {fmtPct(r.pct_change, true)}
          </span>
        </span>
        <span className="flex items-center gap-2">
          <span className="h-1.5 w-12 overflow-hidden rounded bg-neutral-800">
            <span
              className={`block h-full ${scoreColor}`}
              style={{ width: `${Math.min(100, Math.max(0, r.score))}%` }}
            />
          </span>
          <span className="num w-8 text-right">{fmtNumber(r.score)}</span>
        </span>
      </button>
    </li>
  )
}
