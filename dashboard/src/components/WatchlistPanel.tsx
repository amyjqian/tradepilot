import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchWatchlist, runScan, saveWatchlist } from '../api'
import type { ScanResponse, ScanResult } from '../types'
import { fmtNumber, fmtPct } from '../format'
import { useResultFilters } from '../useResultFilters'
import { useCountdown } from '../useCountdown'
import { ResultFilters } from './ResultFilters'
import { AutoRescanStrip } from './AutoRescanStrip'

interface Props {
  provider: string
  interval: string
  lookback: number
  selected: string | null
  onSelect: (r: ScanResult) => void
  onError: (msg: string | null) => void
}

// Same auto-rescan presets as the sector panel; 0 = off. Persisted
// per-user in localStorage so the cadence survives reloads / tab
// switches. The timer keeps cycling while the user is on another tab
// because the panel stays mounted (see LeftRail's PanelHost).
const RESCAN_OPTIONS = [0, 1, 2, 5, 15, 30, 60] as const
const RESCAN_KEY = 'tradepilot.watchlist_rescan_min'

function loadStoredRescan(): number {
  const n = Number(window.localStorage.getItem(RESCAN_KEY))
  return RESCAN_OPTIONS.includes(n as (typeof RESCAN_OPTIONS)[number]) ? n : 0
}

export function WatchlistPanel({
  provider,
  interval,
  lookback,
  selected,
  onSelect,
  onError,
}: Props) {
  const [tickers, setTickers] = useState<string[]>([])
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [scan, setScan] = useState<ScanResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [rescanMin, setRescanMinState] = useState<number>(() => loadStoredRescan())
  const [lastRunAt, setLastRunAt] = useState<Date | null>(null)
  const [nextRunAt, setNextRunAt] = useState<Date | null>(null)
  const [runStartedAt, setRunStartedAt] = useState<Date | null>(null)
  const [runEndedAt, setRunEndedAt] = useState<Date | null>(null)
  const inFlightRef = useRef(false)
  const filters = useResultFilters()
  const countdown = useCountdown(lastRunAt, nextRunAt, rescanMin > 0)

  const setRescanMin = (n: number) => {
    setRescanMinState(n)
    window.localStorage.setItem(RESCAN_KEY, String(n))
  }

  useEffect(() => {
    fetchWatchlist()
      .then((t) => setTickers(t))
      .catch((e) => onError(String(e)))
  }, [onError])

  const run = useCallback(async () => {
    if (tickers.length === 0) {
      // Auto-rescan firing on an empty watchlist would spam this
      // error; only show it when the user explicitly clicks.
      if (!inFlightRef.current) onError('Add tickers to the watchlist first.')
      return
    }
    if (inFlightRef.current) return
    inFlightRef.current = true
    const startedAt = new Date()
    setRunStartedAt(startedAt)
    setRunEndedAt(null)
    onError(null)
    setLoading(true)
    try {
      const res = await runScan(provider, {
        interval,
        lookback_days: lookback,
        tickers,
      })
      setScan(res)
      setLastRunAt(new Date())
    } catch (e) {
      onError(String(e))
    } finally {
      setLoading(false)
      setRunEndedAt(new Date())
      inFlightRef.current = false
    }
  }, [provider, interval, lookback, tickers, onError])

  // Auto-rescan, aligned to wall-clock boundaries + 5 seconds. See
  // SectorRotationPanel for the rationale; same self-rescheduling
  // setTimeout pattern to avoid drift over long sessions.
  useEffect(() => {
    if (rescanMin <= 0) {
      setNextRunAt(null)
      return
    }
    if (tickers.length === 0) {
      setNextRunAt(null)
      return
    }
    if (!scan && !inFlightRef.current) void run()

    let timeoutId: number | null = null
    const schedule = () => {
      const period = rescanMin * 60_000
      const now = Date.now()
      const boundary = Math.floor(now / period) * period + period
      const target = boundary + 5_000
      setNextRunAt(new Date(target))
      timeoutId = window.setTimeout(() => {
        timeoutId = null
        void run()
        schedule()
      }, Math.max(0, target - now))
    }
    schedule()

    return () => {
      if (timeoutId !== null) window.clearTimeout(timeoutId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rescanMin, run, tickers.length])

  const startEdit = () => {
    setDraft(tickers.join(', '))
    setEditing(true)
  }

  const saveEdit = useCallback(async () => {
    const parsed = draft
      .split(/[\s,]+/)
      .map((t) => t.trim().toUpperCase())
      .filter(Boolean)
    try {
      const saved = await saveWatchlist(parsed)
      setTickers(saved)
      setEditing(false)
    } catch (e) {
      onError(String(e))
    }
  }, [draft, onError])

  return (
    <div className="flex h-full flex-col gap-2 overflow-hidden">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={run}
          disabled={loading}
          className="flex-1 rounded bg-[var(--color-accent)] px-3 py-1.5 text-xs font-semibold text-neutral-950 disabled:opacity-50"
        >
          {loading ? 'Scanning…' : `Scan Watchlist (${tickers.length})`}
        </button>
        <label
          className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-neutral-500"
          title="Auto-rescan cadence. Survives tab switches and reloads."
        >
          Auto
          <select
            value={rescanMin}
            onChange={(e) => setRescanMin(Number(e.target.value))}
            className="rounded border border-neutral-700 bg-neutral-900 px-1 py-0.5 text-xs text-neutral-100"
          >
            <option value={0}>Off</option>
            <option value={1}>1m</option>
            <option value={2}>2m</option>
            <option value={5}>5m</option>
            <option value={15}>15m</option>
            <option value={30}>30m</option>
            <option value={60}>60m</option>
          </select>
        </label>
        <button
          type="button"
          onClick={editing ? saveEdit : startEdit}
          className="rounded border border-neutral-700 px-2 py-1.5 text-xs"
          title="Edit watchlist tickers"
        >
          {editing ? 'Save' : 'Edit'}
        </button>
      </div>
      {rescanMin > 0 && (
        <AutoRescanStrip
          rescanMin={rescanMin}
          loading={loading}
          countdown={countdown}
          runStartedAt={runStartedAt}
          runEndedAt={runEndedAt}
        />
      )}

      {editing && (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="AAPL, MSFT, NVDA, …"
          rows={6}
          className="w-full rounded border border-neutral-700 bg-neutral-900 p-2 font-mono text-xs"
        />
      )}

      {!scan && !loading && !editing && (
        <p className="text-xs text-neutral-500">
          Scans your saved ticker list and ranks the top scorers. Add tickers via the Edit
          button — comma- or space-separated, no exchange prefix.
        </p>
      )}

      {scan && (
        <div className="flex flex-1 flex-col overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-1 text-[10px] uppercase tracking-wide text-neutral-500">
            <span>
              Top picks (
              {filters.hasActive
                ? `${filters.apply(scan.results).length} of ${scan.results.length}`
                : scan.results.length}
              )
            </span>
            <span>{scan.n_candidates_scanned} scanned</span>
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
            {(() => {
              const visible = filters.apply(scan.results)
              if (scan.results.length === 0)
                return (
                  <p className="py-2 text-xs text-neutral-500">
                    No tickers passed filters.
                  </p>
                )
              if (visible.length === 0)
                return (
                  <p className="py-2 text-xs text-neutral-500">
                    All {scan.results.length} hits filtered out — clear a pill to
                    see them.
                  </p>
                )
              return (
                <ul className="divide-y divide-neutral-900">
                  {visible.map((r) => (
                    <ScannerRow
                      key={r.ticker}
                      result={r}
                      isSelected={selected === r.ticker}
                      onClick={() => onSelect(r)}
                    />
                  ))}
                </ul>
              )
            })()}
          </div>
        </div>
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
