import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchWatchlist, runScan, saveWatchlist } from '../api'
import type { ScanResponse, ScanResult } from '../types'
import { fmtNumber } from '../format'
import { useResultFilters } from '../useResultFilters'
import { useCountdown } from '../useCountdown'
import { ResultFilters } from './ResultFilters'
import { AutoRescanStrip } from './AutoRescanStrip'
import { LiveChangeBadge } from './LiveChangeBadge'

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

// Interval → auto-rescan minutes. A 5m chart auto-rescans every 5 minutes,
// 15m every 15, etc. — one fresh scan per closed bar. Daily defaults to off
// because there's no closed bar to wait for during the trading day. The user
// can still override in the AUTO dropdown afterwards.
const INTERVAL_RESCAN_MIN: Record<string, number> = {
  '1m': 1,
  '2m': 2,
  '5m': 5,
  '15m': 15,
  '30m': 30,
  '1h': 60,
  '1d': 0,
}

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
  // Bumped on every interval/lookback change so the result of an in-flight
  // scan that started under the old interval gets dropped on arrival rather
  // than overwriting fresh state with stale rows.
  const scanGenRef = useRef(0)
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
    const myGen = scanGenRef.current
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
      // If interval/lookback changed while this fetch was in flight, the
      // result is stale — drop it so we don't paint old-interval rows over
      // the fresh state the change-effect just kicked off.
      if (myGen !== scanGenRef.current) return
      setScan(res)
      setLastRunAt(new Date())
    } catch (e) {
      if (myGen === scanGenRef.current) onError(String(e))
    } finally {
      if (myGen === scanGenRef.current) setLoading(false)
      setRunEndedAt(new Date())
      inFlightRef.current = false
    }
  }, [provider, interval, lookback, tickers, onError])

  // When the user flips interval (or lookback) in the top bar, the existing
  // results are computed for the old timeframe — staring at "MU +4.84% on 1m"
  // and switching to 15m was leaving the old numbers on screen because the
  // auto-rescan effect below only fires run() on its own schedule. Treat a
  // timeframe change as an explicit "show me the new data now" gesture: clear
  // the stale result so the panel acknowledges the change, then re-fetch.
  const initialMountRef = useRef(true)
  useEffect(() => {
    if (initialMountRef.current) {
      initialMountRef.current = false
      return
    }
    if (tickers.length === 0) return
    // Invalidate any in-flight scan and free the gate so the new scan can
    // fire without waiting for the stale one to drain. The stale fetch's
    // result is dropped on arrival via the scanGen check in `run()`.
    scanGenRef.current += 1
    inFlightRef.current = false
    setScan(null)
    // Sync the auto-rescan period to the new bar interval so scans align
    // with closed bars (5m chart → rescan every 5m). Skips for intervals
    // that don't have a sensible default cadence (1d).
    const mapped = INTERVAL_RESCAN_MIN[interval]
    if (mapped !== undefined && mapped !== rescanMin) setRescanMin(mapped)
    void run()
    // run/tickers/rescanMin intentionally excluded — they change every
    // render and would re-fire this loop spuriously. We only want
    // interval/lookback to drive the reset.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interval, lookback])

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
          <LiveChangeBadge ticker={r.ticker} fallbackPct={r.pct_change} />
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
