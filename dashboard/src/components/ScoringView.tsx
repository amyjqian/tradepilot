import { useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchWatchlist,
  runScoringScanMulti,
  runScoringSectorScan,
  saveWatchlist,
  scoringStreamUrl,
  type ScoringRejection,
  type ScoringResult,
  type ScoringSectorScanResponse,
  type ScoringStreamSnapshot,
  type ScoringStreamUpdate,
} from '../api'
import { RealtimeChart } from './RealtimeChart'

type Source = 'watchlist' | 'sectors' | 'defaults'

/** Spec-supported cadences. The math is identical at every value; only
 * the runner's refresh interval differs. See PER_*_MINUTE_SCORING.md. */
const CADENCE_OPTIONS = [
  { seconds: 60, label: '1m' },
  { seconds: 120, label: '2m' },
  { seconds: 300, label: '5m' },
  { seconds: 900, label: '15m' },
] as const

type CadenceSeconds = (typeof CADENCE_OPTIONS)[number]['seconds']

const SIGNAL_ORDER = [
  'rvol_30m',
  'rvol_cumulative',
  'momentum_atr',
  'vwap_distance_atr',
  'trend_stack_5m',
  'mtf_alignment',
  'rsi_intraday',
  'breakout_proximity',
  'clean_structure',
] as const

const TIER_COLOR: Record<string, string> = {
  A: 'bg-emerald-500 text-emerald-950',
  B: 'bg-amber-500 text-amber-950',
  C: 'bg-neutral-500 text-neutral-100',
  none: 'bg-neutral-800 text-neutral-500',
}

interface PanelState {
  results: ScoringResult[]
  rejected: ScoringRejection[]
  lastTickAt: number | null
  /** Wall-clock-ish ms timestamp the panel's score was computed against.
   * For one-shot, this is the cadence-aligned boundary. For live, it's
   * the runner's cycle timestamp. */
  evalMs: number | null
}

type Panels = Record<CadenceSeconds, PanelState>

const EMPTY_PANEL: PanelState = {
  results: [],
  rejected: [],
  lastTickAt: null,
  evalMs: null,
}

function makeEmptyPanels(): Panels {
  return {
    60: { ...EMPTY_PANEL },
    120: { ...EMPTY_PANEL },
    300: { ...EMPTY_PANEL },
    900: { ...EMPTY_PANEL },
  }
}

interface Selection {
  result: ScoringResult
  cadence: CadenceSeconds
}

interface Props {
  provider: string
}

export function ScoringView({ provider }: Props) {
  const [panels, setPanels] = useState<Panels>(makeEmptyPanels())
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [selected, setSelected] = useState<Selection | null>(null)
  const [watchlist, setWatchlist] = useState<string[]>([])
  const [source, setSource] = useState<Source>('watchlist')
  const [sectors, setSectors] =
    useState<ScoringSectorScanResponse['sectors'] | null>(null)
  /** Drill-into-sector mode: when a top-sector ribbon chip is clicked,
   * the 4 cadence panels filter to that ETF's constituents only and the
   * right-pane chart swaps to the ETF itself (until a constituent is
   * clicked, which re-claims the chart). */
  const [selectedSector, setSelectedSector] = useState<string | null>(null)
  const [liveActive, setLiveActive] = useState(false)
  const [scannedAt, setScannedAt] = useState<string | null>(null)
  const [lastFocusedCadence, setLastFocusedCadence] = useState<CadenceSeconds>(300)
  /** Verify @ time mode — when non-empty, one-shot Run Scan pins
   * evaluation to this timestamp instead of `now`. The string format is
   * `YYYY-MM-DDTHH:MM` (HTML datetime-local input value). */
  const [verifyAt, setVerifyAt] = useState<string>('')

  // EventSource handles per cadence — live mode opens 4 connections, one
  // for each cadence panel. Browsers cap HTTP/1.1 at ~6 connections per
  // origin, so plus the global quotes/warnings streams we're at 5–6. If
  // this becomes a problem, multiplex all 4 cadences server-side.
  const eventSourcesRef = useRef<Map<CadenceSeconds, EventSource>>(new Map())

  // Refresh on mount so the user sees the current watchlist size before
  // they hit "run scan." Edits in the Trade tab show up the next time
  // this view mounts.
  useEffect(() => {
    fetchWatchlist()
      .then(setWatchlist)
      .catch(() => {
        setErr('Could not load watchlist; scan will use default tickers.')
      })
  }, [])

  function updatePanel(cadence: CadenceSeconds, patch: Partial<PanelState>) {
    setPanels((prev) => ({ ...prev, [cadence]: { ...prev[cadence], ...patch } }))
  }

  async function run() {
    setLoading(true)
    setErr(null)
    setSectors(null)
    const t0 = performance.now()
    try {
      if (source === 'sectors') {
        // Sectors mode now also emits per-cadence panels (same shape as
        // scan-multi) so each cadence evaluates at its own boundary,
        // and verify@time threads through to the constituent scoring.
        const evalAtMs = parseVerifyAtToMs(verifyAt)
        const resp = await runScoringSectorScan(provider, {
          top_sectors: 2,
          top_n: 50,
          cadences: CADENCE_OPTIONS.map((o) => o.seconds),
          ...(evalAtMs !== null ? { eval_at_ms: evalAtMs } : {}),
        })
        setSectors(resp.sectors)
        const now = Date.now()
        const newPanels: Panels = { ...panels }
        for (const opt of CADENCE_OPTIONS) {
          const panel = resp.panels[String(opt.seconds)]
          if (!panel) {
            newPanels[opt.seconds] = {
              results: [],
              rejected: [],
              lastTickAt: now,
              evalMs: null,
            }
            continue
          }
          newPanels[opt.seconds] = {
            results: panel.rankings,
            rejected: panel.rejected,
            lastTickAt: now,
            evalMs: panel.eval_ms,
          }
        }
        setPanels(newPanels)
        const focusPanel = resp.panels[String(lastFocusedCadence)]
        applySelectionFromRankings(focusPanel?.rankings ?? [])
      } else {
        let tickers: string[] | undefined
        if (source === 'watchlist') {
          try {
            tickers = await fetchWatchlist()
            setWatchlist(tickers)
          } catch {
            /* server falls back to defaults */
          }
        }
        const evalAtMs = parseVerifyAtToMs(verifyAt)
        const resp = await runScoringScanMulti(provider, {
          top_n: 50,
          cadences: CADENCE_OPTIONS.map((o) => o.seconds),
          ...(tickers && tickers.length ? { tickers } : {}),
          ...(evalAtMs !== null ? { eval_at_ms: evalAtMs } : {}),
        })
        const now = Date.now()
        const newPanels: Panels = { ...panels }
        for (const opt of CADENCE_OPTIONS) {
          const panel = resp.panels[String(opt.seconds)]
          if (!panel) continue
          newPanels[opt.seconds] = {
            results: panel.rankings,
            rejected: panel.rejected,
            lastTickAt: now,
            evalMs: panel.eval_ms,
          }
        }
        setPanels(newPanels)
        // Pick selection from the user's last-focused panel.
        const focusPanel = resp.panels[String(lastFocusedCadence)]
        applySelectionFromRankings(focusPanel?.rankings ?? [])
      }
      const elapsed = ((performance.now() - t0) / 1000).toFixed(1)
      setScannedAt(`${new Date().toISOString()} · ${elapsed}s`)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  function applySelectionFromRankings(rankings: ScoringResult[]) {
    setSelected((prev) => {
      if (prev) {
        const match = rankings.find((r) => r.symbol === prev.result.symbol)
        if (match) return { result: match, cadence: prev.cadence }
      }
      const top = rankings[0]
      return top ? { result: top, cadence: lastFocusedCadence } : null
    })
  }

  function applyStreamPayload(
    cadence: CadenceSeconds,
    payload: ScoringStreamUpdate | ScoringStreamSnapshot,
  ) {
    const now = Date.now()
    const evalMs =
      'timestamp_ms' in payload && typeof payload.timestamp_ms === 'number'
        ? payload.timestamp_ms
        : now
    updatePanel(cadence, {
      results: payload.rankings,
      rejected: 'rejected' in payload ? payload.rejected : [],
      lastTickAt: now,
      evalMs,
    })
    // If the user's selected symbol came from this cadence and is still
    // present in the new rankings, freshen the result reference. Don't
    // hop selection across panels on update.
    setSelected((prev) => {
      if (!prev || prev.cadence !== cadence) return prev
      const match = payload.rankings.find((r) => r.symbol === prev.result.symbol)
      return match ? { result: match, cadence } : prev
    })
  }

  function stopLive() {
    for (const es of eventSourcesRef.current.values()) {
      es.close()
    }
    eventSourcesRef.current.clear()
    setLiveActive(false)
  }

  async function startLive() {
    if (source === 'sectors') {
      setErr('Live mode not available for the Top Sectors source — use Run Scan.')
      return
    }
    setErr(null)
    let tickers: string[] | undefined
    if (source === 'watchlist') {
      try {
        tickers = await fetchWatchlist()
        setWatchlist(tickers)
      } catch {
        /* server uses defaults */
      }
    }
    stopLive()
    setScannedAt(null)
    for (const opt of CADENCE_OPTIONS) {
      const url = scoringStreamUrl(provider, {
        tickers,
        cadenceSeconds: opt.seconds,
        live: provider === 'polygon',
      })
      const es = new EventSource(url)
      eventSourcesRef.current.set(opt.seconds, es)
      es.addEventListener('snapshot', (ev) => {
        try {
          const payload = JSON.parse(
            (ev as MessageEvent).data,
          ) as ScoringStreamSnapshot
          applyStreamPayload(opt.seconds, payload)
        } catch {
          /* malformed snapshot — ignore */
        }
      })
      es.onmessage = (ev) => {
        try {
          const payload = JSON.parse(ev.data) as ScoringStreamUpdate
          applyStreamPayload(opt.seconds, payload)
        } catch {
          /* malformed update — ignore */
        }
      }
      es.onerror = () => {
        // One stream failing doesn't necessarily kill the others — but
        // surface the error and let the user decide. Don't auto-stop
        // siblings since they might still be flowing.
        setErr(`Live stream connection error on ${opt.label} cadence.`)
      }
    }
    setLiveActive(true)
  }

  // Tear down on unmount; reset on provider change.
  useEffect(() => {
    return () => stopLive()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    if (liveActive) stopLive()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider])

  /** When a top-sector ETF is drilled-into, every panel + the Filtered
   * section narrow to that ETF's constituents. The full panels are
   * still in `panels`; this is a view layer. */
  const filteredPanels = useMemo<Panels>(() => {
    if (!selectedSector || !sectors) return panels
    const allowed = new Set(
      sectors.constituents_by_sector[selectedSector] ?? [],
    )
    const out = makeEmptyPanels()
    for (const opt of CADENCE_OPTIONS) {
      const p = panels[opt.seconds]
      out[opt.seconds] = {
        ...p,
        results: p.results.filter((r) => allowed.has(r.symbol)),
        rejected: p.rejected.filter((r) => allowed.has(r.symbol)),
      }
    }
    return out
  }, [panels, sectors, selectedSector])

  const totalRanked = CADENCE_OPTIONS.reduce(
    (sum, opt) => sum + filteredPanels[opt.seconds].results.length,
    0,
  )
  const totalTierEligible = CADENCE_OPTIONS.reduce(
    (sum, opt) =>
      sum +
      filteredPanels[opt.seconds].results.filter((r) => r.tier !== null).length,
    0,
  )

  /** Cross-cadence consensus picks. The spec recommends this pattern in
   * `PER_15_MINUTE_SCORING.md` §7: "names that appear in BOTH lists are
   * the highest-conviction ideas." We extend it across all 4 cadences,
   * scoring each symbol by:
   *
   *   - appearances (count of cadences ranking it)        → up to +40
   *   - best tier achieved (A=+30, B=+20, C=+10)          → up to +30
   *   - average final_score across appearances            → up to +100
   *
   * Top 5 by this combined metric. Filtered through the same view
   * (drilldown sector / source) so the picks reflect what's on screen.
   */
  const topPicks = useMemo(() => {
    interface PickAgg {
      symbol: string
      cadences: CadenceSeconds[]
      scoreSum: number
      bestTier: 'A' | 'B' | 'C' | null
      bestResult: ScoringResult
    }
    const aggs: Record<string, PickAgg> = {}
    const tierRank = (t: string | null): number =>
      t === 'A' ? 3 : t === 'B' ? 2 : t === 'C' ? 1 : 0
    for (const opt of CADENCE_OPTIONS) {
      for (const r of filteredPanels[opt.seconds].results) {
        const a = (aggs[r.symbol] ??= {
          symbol: r.symbol,
          cadences: [],
          scoreSum: 0,
          bestTier: null,
          bestResult: r,
        })
        a.cadences.push(opt.seconds)
        a.scoreSum += r.final_score
        if (tierRank(r.tier) > tierRank(a.bestTier)) {
          a.bestTier = r.tier
          a.bestResult = r
        }
      }
    }
    const scored = Object.values(aggs)
      .map((a) => {
        const avgScore = a.scoreSum / a.cadences.length
        const tierBonus =
          a.bestTier === 'A'
            ? 30
            : a.bestTier === 'B'
              ? 20
              : a.bestTier === 'C'
                ? 10
                : 0
        const appearanceBonus = a.cadences.length * 10 // up to 40
        return {
          ...a,
          avgScore,
          consensus: avgScore + tierBonus + appearanceBonus,
        }
      })
      .filter((a) => a.cadences.length > 0)
      .sort((a, b) => b.consensus - a.consensus)
    return scored.slice(0, 5)
  }, [filteredPanels])

  /** Build a per-symbol map of {ranked-in-which-cadences, rejection-reasons,
   * not-found-in-data}. Useful to answer "why isn't MU in the list?" — the
   * answer is either: (a) it's ranked in some cadence (highlight it),
   * (b) it was filtered with these reasons, or (c) Polygon didn't return
   * data for it. */
  const symbolStatus = useMemo(() => {
    const ranked: Record<string, CadenceSeconds[]> = {}
    const rejectionsBySymbol: Record<string, ScoringRejection[]> = {}
    for (const opt of CADENCE_OPTIONS) {
      const p = filteredPanels[opt.seconds]
      for (const r of p.results) {
        ;(ranked[r.symbol] ??= []).push(opt.seconds)
      }
      for (const rej of p.rejected) {
        ;(rejectionsBySymbol[rej.symbol] ??= []).push(rej)
      }
    }
    // Source set = symbols we expected to see. Watchlist → user's list.
    // Top Sectors with a drill-into-ETF → that ETF's constituents.
    // Top Sectors with no drill → all top sectors' constituents.
    // Otherwise → union of ranked + rejected (best we can do).
    let expectedSource: string[]
    if (source === 'watchlist') {
      expectedSource = watchlist
    } else if (source === 'sectors' && sectors) {
      expectedSource = selectedSector
        ? (sectors.constituents_by_sector[selectedSector] ?? [])
        : sectors.top_etfs.flatMap(
            (etf) => sectors.constituents_by_sector[etf] ?? [],
          )
    } else {
      expectedSource = [
        ...new Set([
          ...Object.keys(ranked),
          ...Object.keys(rejectionsBySymbol),
        ]),
      ]
    }
    const filtered: Array<{
      symbol: string
      reasons: string[]
      kind: 'rejected' | 'no-data'
    }> = []
    for (const sym of expectedSource) {
      if (sym in ranked) continue // it's in some panel's rankings
      if (sym in rejectionsBySymbol) {
        const reasons = new Set<string>()
        for (const rej of rejectionsBySymbol[sym]) {
          for (const reason of rej.reasons) reasons.add(reason)
        }
        filtered.push({
          symbol: sym,
          reasons: [...reasons].sort(),
          kind: 'rejected',
        })
      } else {
        // In the watchlist but absent from both rankings and rejections
        // → Polygon didn't return data (delisted, ticker change, fetch
        // error). The build_states warning logs this server-side.
        filtered.push({
          symbol: sym,
          reasons: ['no_provider_data'],
          kind: 'no-data',
        })
      }
    }
    filtered.sort((a, b) => a.symbol.localeCompare(b.symbol))
    return { ranked, filtered }
  }, [filteredPanels, watchlist, source, sectors, selectedSector])

  return (
    <div className="flex h-full min-h-0 gap-3 overflow-hidden p-3">
      <section className="flex min-h-0 flex-[3] flex-col gap-2 rounded border border-neutral-800 bg-neutral-950 p-3">
        <header className="flex items-center justify-between gap-3">
          <div className="flex min-w-0 items-baseline gap-3">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-200">
              Per-Minute Scoring · 4 cadences
            </h2>
            <span className="truncate text-xs text-neutral-500">
              {liveActive ? (
                <>
                  <span className="mr-1 inline-block h-2 w-2 animate-pulse rounded-full bg-red-500 align-middle" />
                  LIVE · 4 streams · {totalRanked} ranked across all panels
                </>
              ) : loading ? (
                'scoring all cadences…'
              ) : verifyAt ? (
                <>
                  <span className="mr-1 rounded bg-amber-900/60 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-amber-200">
                    verify
                  </span>
                  evaluating @ {verifyAt.replace('T', ' ')} ET
                </>
              ) : scannedAt ? (
                `${scannedAt.split(' · ')[1] ?? ''}`
              ) : (
                'not yet run'
              )}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-1">
            <SourceChip
              active={source === 'watchlist'}
              onClick={() => {
                setSource('watchlist')
                setSelectedSector(null)
              }}
              label={`Watchlist${watchlist.length ? ` (${watchlist.length})` : ''}`}
            />
            <SourceChip
              active={source === 'sectors'}
              onClick={() => setSource('sectors')}
              label="Top Sectors"
            />
            <SourceChip
              active={source === 'defaults'}
              onClick={() => {
                setSource('defaults')
                setSelectedSector(null)
              }}
              label="Defaults"
            />
            <span className="ml-2 mr-1 text-[10px] uppercase tracking-wide text-neutral-600">
              verify@
            </span>
            <input
              type="datetime-local"
              value={verifyAt}
              onChange={(e) => setVerifyAt(e.target.value)}
              disabled={liveActive}
              title="Pin one-shot Run Scan to a past ET timestamp for verification. Empty = use current wall clock. Up to 30 days back via auto-extended history; pass intraday_lookback_days for further."
              className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-[11px] text-neutral-200 disabled:opacity-40"
            />
            {verifyAt && (
              <button
                type="button"
                onClick={() => setVerifyAt('')}
                title="Clear verify time"
                className="rounded bg-neutral-800 px-2 py-1 text-[11px] text-neutral-400 hover:text-neutral-200"
              >
                ×
              </button>
            )}
            <button
              type="button"
              onClick={run}
              disabled={loading || liveActive}
              title={liveActive ? 'Stop live mode first' : 'Run a single scan across all cadences'}
              className="ml-2 rounded bg-neutral-700 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-neutral-100 disabled:opacity-40"
            >
              {loading ? 'scoring…' : verifyAt ? 'verify' : 'run scan'}
            </button>
            <button
              type="button"
              onClick={liveActive ? stopLive : startLive}
              disabled={loading || (source === 'sectors' && !liveActive) || Boolean(verifyAt)}
              title={
                verifyAt
                  ? 'Live mode disabled while verify @ time is set — clear the timestamp first'
                  : source === 'sectors'
                    ? 'Top Sectors source is one-shot only'
                    : liveActive
                      ? 'Stop all 4 streams'
                      : 'Stream all 4 cadences live'
              }
              className={`rounded px-3 py-1 text-xs font-semibold uppercase tracking-wide disabled:opacity-40 ${
                liveActive
                  ? 'bg-red-600 text-neutral-50'
                  : 'bg-[var(--color-accent)] text-neutral-950'
              }`}
            >
              {liveActive ? 'stop live' : 'start live'}
            </button>
          </div>
        </header>
        {verifyAt && (() => {
          const preview = verifyBoundariesPreview(verifyAt)
          if (!preview) return null
          const suggestion = !preview.diverges
            ? suggestDivergentVerifyAt(verifyAt)
            : null
          return (
            <div className="flex flex-wrap items-center gap-2 rounded border border-amber-900/60 bg-amber-950/30 px-2 py-1 text-[11px] text-amber-200">
              <span className="text-[10px] uppercase tracking-wide text-amber-400/80">
                boundary preview
              </span>
              <span className="font-mono">{preview.text}</span>
              {!preview.diverges && (
                <>
                  <span className="text-amber-400">
                    ⚠ all cadences align here — scores will be identical across panels.
                  </span>
                  {suggestion && (
                    <button
                      type="button"
                      onClick={() => setVerifyAt(suggestion)}
                      className="rounded bg-amber-700 px-2 py-0.5 font-semibold text-amber-50 hover:bg-amber-600"
                      title="Shift verify time to the closest non-aligned minute"
                    >
                      Use {suggestion.split('T')[1]} instead →
                    </button>
                  )}
                </>
              )}
            </div>
          )
        })()}
        {sectors && (
          <div className="rounded border border-neutral-800 bg-neutral-900/50 p-2 text-xs">
            <div className="mb-1 flex items-baseline justify-between text-[10px] uppercase tracking-wide text-neutral-500">
              <span>Top sectors (by 5-bar excess vs SPY)</span>
              {selectedSector && (
                <button
                  type="button"
                  onClick={() => setSelectedSector(null)}
                  className="rounded bg-neutral-800 px-2 py-0.5 normal-case text-neutral-300 hover:bg-neutral-700"
                  title="Show all top sectors' constituents"
                >
                  Showing {selectedSector} only · clear
                </button>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {sectors.ranked.slice(0, 5).map((r, i) => {
                const isTop = sectors.top_etfs.includes(r.etf)
                const isSelectedSector = selectedSector === r.etf
                const clickable = isTop // only top-N are filterable
                return (
                  <button
                    key={r.etf}
                    type="button"
                    disabled={!clickable}
                    onClick={() => {
                      // Clicking a sector chip claims the chart for the
                      // ETF. Clear any constituent selection so the
                      // right pane swaps to the ETF chart immediately
                      // (otherwise the prior constituent would keep
                      // priority and the user wouldn't see the ETF).
                      if (isSelectedSector) {
                        setSelectedSector(null)
                      } else {
                        setSelectedSector(r.etf)
                        setSelected(null)
                      }
                    }}
                    title={
                      clickable
                        ? `Click to drill into ${r.etf}'s constituents`
                        : `${r.etf} is not in the top sectors`
                    }
                    className={`flex items-center gap-1 rounded px-2 py-0.5 transition-colors ${
                      isSelectedSector
                        ? 'bg-emerald-700 text-emerald-50 ring-2 ring-emerald-400'
                        : isTop
                          ? 'bg-emerald-900/60 text-emerald-200 hover:bg-emerald-800/70'
                          : 'bg-neutral-800 text-neutral-400'
                    } ${!clickable ? 'cursor-default' : 'cursor-pointer'}`}
                  >
                    <span className="font-mono">{r.etf}</span>
                    <span className="text-neutral-500">{r.name}</span>
                    <span className="tabular-nums">
                      {(r.excess_return_5_vs_spy * 100).toFixed(2)}%
                    </span>
                    {isTop && (
                      <span className="text-[10px] uppercase">
                        ({sectors.constituents_by_sector[r.etf]?.length ?? 0})
                      </span>
                    )}
                    {i === 0 && !isSelectedSector && (
                      <span className="text-[10px]">★</span>
                    )}
                  </button>
                )
              })}
            </div>
          </div>
        )}
        {err && (
          <div className="rounded border border-red-700 bg-red-950/40 px-2 py-1 text-xs text-red-300">
            {err}
          </div>
        )}
        {totalRanked > 0 && totalTierEligible === 0 && (
          <div className="rounded border border-amber-900/60 bg-amber-950/40 px-2 py-1 text-[11px] text-amber-200">
            <span className="mr-1 rounded bg-amber-700 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-50">
              No actionable setups
            </span>
            {totalRanked} symbols ranked across the 4 panels but{' '}
            <span className="font-semibold">none cleared Tier C (≥ 70)</span>.
            The rankings below are relative ordering only — rows are dimmed.
            The engine is saying "wait for fresher data" or "today's market
            doesn't have a clean entry."
          </div>
        )}
        <div className="flex min-h-0 flex-[5] gap-2">
          {CADENCE_OPTIONS.map((opt) => (
            <CadencePanel
              key={opt.seconds}
              cadence={opt.seconds}
              label={opt.label}
              state={filteredPanels[opt.seconds]}
              loading={loading}
              liveActive={liveActive}
              isFocused={lastFocusedCadence === opt.seconds}
              selectedSymbol={selected?.result.symbol ?? null}
              isPrimaryForSelection={selected?.cadence === opt.seconds}
              onSelect={(result) => {
                setLastFocusedCadence(opt.seconds)
                setSelected({ result, cadence: opt.seconds })
              }}
              onFocus={() => setLastFocusedCadence(opt.seconds)}
            />
          ))}
        </div>
        {topPicks.length > 0 && (
          <TopPicksPanel
            picks={topPicks}
            selectedSymbol={selected?.result.symbol ?? null}
            currentWatchlist={watchlist}
            onPickSymbol={(pick) => {
              setLastFocusedCadence(pick.cadences[0] ?? lastFocusedCadence)
              setSelected({
                result: pick.bestResult,
                cadence: pick.cadences[0] ?? lastFocusedCadence,
              })
            }}
            onAddToWatchlist={async (symbols) => {
              const merged = Array.from(new Set([...watchlist, ...symbols]))
              try {
                const saved = await saveWatchlist(merged)
                setWatchlist(saved)
              } catch (e) {
                setErr(e instanceof Error ? e.message : String(e))
              }
            }}
          />
        )}
        {symbolStatus.filtered.length > 0 && (
          <FilteredPanel filtered={symbolStatus.filtered} />
        )}
      </section>

      <aside className="flex min-h-0 flex-[2] flex-col gap-2 rounded border border-neutral-800 bg-neutral-950 p-3">
        <header className="flex items-baseline justify-between">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-neutral-200">
            {selected
              ? selected.result.symbol
              : selectedSector
                ? `${selectedSector} ETF`
                : 'Breakdown'}
          </h3>
          {selected ? (
            <span className="text-xs text-neutral-500">
              {cadenceLabel(selected.cadence)} · {selected.result.tier ?? '–'} ·{' '}
              {selected.result.final_score.toFixed(2)}
            </span>
          ) : (
            selectedSector &&
            sectors && (
              <span className="text-xs text-neutral-500">
                {sectors.ranked.find((r) => r.etf === selectedSector)?.name ?? ''}
                {' · '}
                {sectors.constituents_by_sector[selectedSector]?.length ?? 0}{' '}
                holdings
              </span>
            )
          )}
        </header>
        {selected ? (
          <>
            <div className="min-h-0 flex-[3] overflow-hidden rounded border border-neutral-800">
              <RealtimeChart
                key={`${selected.result.symbol}-${selected.cadence}`}
                ticker={selected.result.symbol}
                lockedInterval={cadenceToInterval(selected.cadence)}
                viewWindowHours={4}
                ema9Color="#ffffff"
              />
            </div>
            <div className="min-h-0 flex-[2] overflow-auto">
              <table className="w-full text-xs">
                <thead className="text-[10px] uppercase tracking-wide text-neutral-500">
                  <tr>
                    <th className="p-1 text-left">Signal</th>
                    <th className="p-1">Strength</th>
                    <th className="p-1 text-right">Raw</th>
                  </tr>
                </thead>
                <tbody>
                  {SIGNAL_ORDER.map((name) => {
                    const c = selected.result.components[name]
                    if (!c) return null
                    const pct = Math.round(c.strength * 100)
                    return (
                      <tr key={name} className="border-b border-neutral-900">
                        <td className="p-1 font-mono text-neutral-300">{name}</td>
                        <td className="p-1">
                          <div className="flex items-center gap-2">
                            <div className="h-1.5 w-24 rounded bg-neutral-800">
                              <div
                                className="h-full rounded bg-[var(--color-accent)]"
                                style={{ width: `${pct}%` }}
                              />
                            </div>
                            <span className="tabular-nums text-neutral-400">
                              {pct}
                            </span>
                          </div>
                        </td>
                        <td className="p-1 text-right tabular-nums text-neutral-500">
                          {c.raw === null ? '–' : c.raw.toFixed(3)}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </>
        ) : selectedSector ? (
          <>
            <div className="min-h-0 flex-[3] overflow-hidden rounded border border-neutral-800">
              <RealtimeChart
                key={`${selectedSector}-etf`}
                ticker={selectedSector}
                viewWindowHours={4}
                ema9Color="#ffffff"
              />
            </div>
            <div className="min-h-0 flex-[2] overflow-auto p-2 text-xs text-neutral-400">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-neutral-500">
                {selectedSector} holdings · click a row in any cadence panel for
                its 9-signal breakdown
              </div>
              <div className="font-mono text-[11px] leading-relaxed text-neutral-500">
                {sectors?.constituents_by_sector[selectedSector]?.join(' · ')}
              </div>
            </div>
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center text-xs text-neutral-500">
            Pick a symbol to see its chart and 9-signal breakdown.
          </div>
        )}
      </aside>
    </div>
  )
}

interface CadencePanelProps {
  cadence: CadenceSeconds
  label: string
  state: PanelState
  loading: boolean
  liveActive: boolean
  isFocused: boolean
  selectedSymbol: string | null
  /** True when this panel is the cadence the selection originated from.
   * Primary gets a brighter highlight; non-primary panels showing the
   * same symbol get a subtler "mirror" highlight so the user can see
   * where the same ticker ranks across all 4 cadences. */
  isPrimaryForSelection: boolean
  onSelect: (r: ScoringResult) => void
  onFocus: () => void
}

function CadencePanel({
  label,
  state,
  loading,
  liveActive,
  isFocused,
  selectedSymbol,
  isPrimaryForSelection,
  onSelect,
  onFocus,
}: CadencePanelProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const rowRefs = useRef<Map<string, HTMLTableRowElement>>(new Map())

  // Auto-focus this panel when it becomes the focus target so ↑/↓ work
  // without a click first.
  useEffect(() => {
    if (isFocused && state.results.length > 0) {
      scrollRef.current?.focus({ preventScroll: true })
    }
  }, [isFocused, state.results.length])

  // Scroll selected row into view when it changes.
  useEffect(() => {
    if (!selectedSymbol) return
    rowRefs.current.get(selectedSymbol)?.scrollIntoView({ block: 'nearest' })
  }, [selectedSymbol])

  function handleKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (state.results.length === 0) return
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const idx = selectedSymbol
      ? state.results.findIndex((r) => r.symbol === selectedSymbol)
      : -1
    const next =
      e.key === 'ArrowDown'
        ? Math.min(state.results.length - 1, idx < 0 ? 0 : idx + 1)
        : Math.max(0, idx < 0 ? 0 : idx - 1)
    const nextResult = state.results[next]
    if (nextResult) onSelect(nextResult)
  }

  return (
    <div
      onClick={onFocus}
      className={`flex min-w-0 min-h-0 flex-1 flex-col rounded border bg-neutral-950 ${
        isFocused ? 'border-neutral-600' : 'border-neutral-800'
      }`}
    >
      <header className="flex items-baseline justify-between border-b border-neutral-800 px-2 py-1">
        <div className="flex items-baseline gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-neutral-200">
            {label}
          </span>
          {state.evalMs !== null && (
            <span
              className="text-[10px] tabular-nums text-neutral-600"
              title="The cadence-aligned timestamp this panel's score was computed against."
            >
              @ {new Date(state.evalMs).toLocaleTimeString([], {
                hour: '2-digit',
                minute: '2-digit',
              })}
            </span>
          )}
        </div>
        <div className="flex items-baseline gap-2 text-[10px]">
          {selectedSymbol && (() => {
            const idx = state.results.findIndex((r) => r.symbol === selectedSymbol)
            if (idx === -1) {
              return (
                <span className="rounded bg-neutral-900 px-1 py-0.5 text-neutral-600">
                  {selectedSymbol}: not ranked
                </span>
              )
            }
            return (
              <span
                className={`rounded px-1 py-0.5 font-semibold ${
                  isPrimaryForSelection
                    ? 'bg-[var(--color-accent)]/30 text-[var(--color-accent)] ring-1 ring-[var(--color-accent)]'
                    : 'bg-[var(--color-accent)]/15 text-[var(--color-accent)]'
                }`}
              >
                {selectedSymbol} #{idx + 1}
              </span>
            )
          })()}
          <span className="text-neutral-500">{state.results.length} ranked</span>
        </div>
      </header>
      <div
        ref={scrollRef}
        tabIndex={0}
        onKeyDown={handleKeyDown}
        className="min-h-0 flex-1 overflow-auto outline-none focus:ring-1 focus:ring-neutral-700"
      >
        <table className="w-full text-[11px]">
          <thead className="sticky top-0 bg-neutral-950 text-[9px] uppercase tracking-wide text-neutral-500">
            <tr>
              <th className="p-1 text-left">T</th>
              <th className="p-1 text-left">Sym</th>
              <th className="p-1 text-right">Score</th>
              <th className="p-1 text-left">Bias</th>
            </tr>
          </thead>
          <tbody>
            {state.results.map((r) => {
              const isSel = selectedSymbol === r.symbol
              const tierKey = r.tier ?? 'none'
              // Rows without a tier (final_score < 70 AND/OR bias not
              // long for A/B) are *not* engine recommendations — they're
              // just relative ordering. Mute them visually so the eye
              // doesn't read rank #1 of a no-tier list as "buy."
              const noTier = r.tier === null
              // Cross-panel selection highlight needs to pop visually so
              // the user can spot the same symbol across 4 panels at a
              // glance. Both states use a saturated accent tint; the
              // primary panel additionally gets a thick left bar + ring
              // to disambiguate from mirrors. Selection always wins over
              // the no-tier dimming so a clicked symbol is always clear.
              const selStyle = isSel
                ? isPrimaryForSelection
                  ? 'bg-[var(--color-accent)]/25 border-l-[3px] border-l-[var(--color-accent)] ring-1 ring-inset ring-[var(--color-accent)]/40'
                  : 'bg-[var(--color-accent)]/12 border-l-[3px] border-l-[var(--color-accent)]/70'
                : noTier
                  ? 'opacity-40'
                  : ''
              return (
                <tr
                  key={r.symbol}
                  ref={(el) => {
                    if (el) rowRefs.current.set(r.symbol, el)
                    else rowRefs.current.delete(r.symbol)
                  }}
                  onClick={() => onSelect(r)}
                  className={`cursor-pointer border-b border-neutral-900 hover:bg-neutral-900 hover:opacity-100 ${selStyle}`}
                >
                  <td className="p-1">
                    <span
                      className={`inline-block w-4 rounded text-center text-[9px] font-bold ${TIER_COLOR[tierKey]}`}
                    >
                      {r.tier ?? '–'}
                    </span>
                  </td>
                  <td className="p-1 font-semibold">{r.symbol}</td>
                  <td className="p-1 text-right tabular-nums">
                    {r.final_score.toFixed(1)}
                  </td>
                  <td className="p-1 text-neutral-400">
                    {r.bias_15m === 'long'
                      ? '↑'
                      : r.bias_15m === 'short'
                        ? '↓'
                        : '–'}
                  </td>
                </tr>
              )
            })}
            {!state.results.length && !loading && (
              <tr>
                <td colSpan={4} className="p-3 text-center text-[10px] text-neutral-600">
                  {state.rejected.length > 0
                    ? `all ${state.rejected.length} filtered`
                    : 'no data yet'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** Find the smallest minute-offset from `verifyAt` (search outward, ±)
 * that yields 4 distinct cadence boundaries. Used as a one-click
 * suggestion when the user picks a 15m-aligned time and gets identical
 * scores across panels. Returns the new "YYYY-MM-DDTHH:MM" string, or
 * null if no offset within 30 minutes works (edge case). */
function suggestDivergentVerifyAt(verifyAt: string): string | null {
  const baseMs = parseVerifyAtToMs(verifyAt)
  if (baseMs === null) return null
  const dayParts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
  }).format(new Date(baseMs))
  const [y, mo, d] = dayParts.split('-').map(Number)
  const noonUtc = Date.UTC(y, mo - 1, d, 12, 0, 0)
  const etHour = Number(
    new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour12: false,
      hour: '2-digit',
    })
      .formatToParts(new Date(noonUtc))
      .find((p) => p.type === 'hour')!.value,
  )
  const offsetHours = etHour - 12
  const sessionMs = Date.UTC(y, mo - 1, d, 9 - offsetHours, 30, 0)
  for (let delta = 1; delta <= 30; delta++) {
    for (const sign of [1, -1] as const) {
      const candidateMs = baseMs + sign * delta * 60_000
      const elapsedMin = Math.floor((candidateMs - sessionMs) / 60_000)
      if (elapsedMin <= 0) continue
      const b1 = elapsedMin
      const b2 = Math.floor(elapsedMin / 2) * 2
      const b5 = Math.floor(elapsedMin / 5) * 5
      const b15 = Math.floor(elapsedMin / 15) * 15
      if (b1 !== b2 && b2 !== b5 && b5 !== b15) {
        return shiftVerifyAt(verifyAt, sign * delta)
      }
    }
  }
  return null
}

function shiftVerifyAt(verifyAt: string, deltaMinutes: number): string {
  const [date, time] = verifyAt.split('T')
  const [h, m] = time.split(':').map(Number)
  const totalMin = h * 60 + m + deltaMinutes
  if (totalMin < 0 || totalMin >= 24 * 60) return verifyAt
  const nh = Math.floor(totalMin / 60).toString().padStart(2, '0')
  const nm = (totalMin % 60).toString().padStart(2, '0')
  return `${date}T${nh}:${nm}`
}

/** Compute the per-cadence boundary timestamps for a given verify time,
 * formatted as "1m 14:42 · 2m 14:42 · 5m 14:40 · 15m 14:30". Used to
 * preview which cadences will diverge before clicking Verify — picking
 * a multiple-of-15 minute (e.g., 14:30) produces identical results
 * because all 4 cadences land on the same boundary. */
function verifyBoundariesPreview(verifyAt: string): {
  text: string
  diverges: boolean
} | null {
  const evalMs = parseVerifyAtToMs(verifyAt)
  if (evalMs === null) return null
  // Compute today's 09:30 ET in epoch ms for the same calendar day.
  const dayPart = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
  }).format(new Date(evalMs))
  const [y, mo, d] = dayPart.split('-').map(Number)
  const noonUtc = Date.UTC(y, mo - 1, d, 12, 0, 0)
  const etHour = Number(
    new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour12: false,
      hour: '2-digit',
    })
      .formatToParts(new Date(noonUtc))
      .find((p) => p.type === 'hour')!.value,
  )
  const offsetHours = etHour - 12
  const sessionStartMs = Date.UTC(y, mo - 1, d, 9 - offsetHours, 30, 0)
  const fmtTime = (ms: number) =>
    new Date(ms).toLocaleTimeString('en-US', {
      timeZone: 'America/New_York',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  const labels = CADENCE_OPTIONS.map((opt) => {
    const cadenceMs = opt.seconds * 1000
    const boundary =
      sessionStartMs + Math.floor((evalMs - sessionStartMs) / cadenceMs) * cadenceMs
    return { opt, boundary }
  })
  const diverges =
    new Set(labels.map((l) => l.boundary)).size > 1
  const text = labels
    .map((l) => `${l.opt.label} ${fmtTime(l.boundary)}`)
    .join(' · ')
  return { text, diverges }
}

/** Parse a `<input type="datetime-local">` value to epoch ms. The
 * input is local-clock (browser timezone); we reinterpret as ET so the
 * verify-mode timestamp lands on the right trading day regardless of
 * where the user's browser is. Returns null on empty/invalid input. */
function parseVerifyAtToMs(s: string): number | null {
  if (!s) return null
  // s is "YYYY-MM-DDTHH:MM" or "YYYY-MM-DDTHH:MM:SS"
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(s)
  if (!m) return null
  const [, y, mo, d, h, min, sec] = m
  // Build the same instant in ET. Use the offset trick: format an UTC
  // candidate in ET and adjust.
  const utcCandidate = Date.UTC(
    Number(y),
    Number(mo) - 1,
    Number(d),
    Number(h),
    Number(min),
    sec ? Number(sec) : 0,
  )
  // What ET hour does this UTC instant render as?
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour12: false,
    hour: '2-digit',
  })
  const etHour = Number(
    fmt.formatToParts(new Date(utcCandidate)).find((p) => p.type === 'hour')!.value,
  )
  const offsetHours = etHour - Number(h)
  return utcCandidate - offsetHours * 3600 * 1000
}

interface TopPick {
  symbol: string
  cadences: CadenceSeconds[]
  bestTier: 'A' | 'B' | 'C' | null
  bestResult: ScoringResult
  avgScore: number
  consensus: number
}

function TopPicksPanel({
  picks,
  selectedSymbol,
  currentWatchlist,
  onPickSymbol,
  onAddToWatchlist,
}: {
  picks: TopPick[]
  selectedSymbol: string | null
  currentWatchlist: string[]
  onPickSymbol: (pick: TopPick) => void
  onAddToWatchlist: (symbols: string[]) => Promise<void>
}) {
  const [adding, setAdding] = useState(false)
  const [added, setAdded] = useState(false)
  const onWatchlist = new Set(currentWatchlist)
  const newPicks = picks.filter((p) => !onWatchlist.has(p.symbol))

  async function handleAdd() {
    setAdding(true)
    try {
      await onAddToWatchlist(picks.map((p) => p.symbol))
      setAdded(true)
      window.setTimeout(() => setAdded(false), 2000)
    } finally {
      setAdding(false)
    }
  }

  return (
    <div className="rounded border border-emerald-900/60 bg-emerald-950/30 p-2 text-xs">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <span className="font-semibold uppercase tracking-wide text-emerald-200">
            Top {picks.length} consensus picks
          </span>
          <span
            className="text-[10px] text-emerald-400/70"
            title="Cross-cadence consensus: appearances + best-tier + average score. The spec recommends this pattern in PER_15_MINUTE_SCORING.md §7."
          >
            cross-cadence math · click to inspect · hover for details
          </span>
        </div>
        <button
          type="button"
          onClick={handleAdd}
          disabled={adding || newPicks.length === 0}
          title={
            newPicks.length === 0
              ? 'All picks are already on the watchlist'
              : `Add ${newPicks.length} new symbol${newPicks.length === 1 ? '' : 's'} to the watchlist`
          }
          className="rounded bg-emerald-700 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-50 hover:bg-emerald-600 disabled:opacity-40"
        >
          {added
            ? '✓ added'
            : adding
              ? 'adding…'
              : newPicks.length > 0
                ? `+ ${newPicks.length} to watchlist`
                : 'all on watchlist'}
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {picks.map((p, i) => {
          const tierClr =
            p.bestTier === 'A'
              ? 'bg-emerald-500 text-emerald-950'
              : p.bestTier === 'B'
                ? 'bg-amber-500 text-amber-950'
                : p.bestTier === 'C'
                  ? 'bg-neutral-400 text-neutral-900'
                  : 'bg-neutral-700 text-neutral-300'
          const isSel = selectedSymbol === p.symbol
          const onWl = onWatchlist.has(p.symbol)
          const cadenceLabels = p.cadences
            .map((c) => CADENCE_OPTIONS.find((o) => o.seconds === c)?.label ?? `${c}s`)
            .join(' · ')
          const tooltip =
            `Best tier ${p.bestTier ?? '–'} ` +
            `· avg score ${p.avgScore.toFixed(1)} ` +
            `· in ${p.cadences.length}/4 cadences (${cadenceLabels}) ` +
            `· consensus ${p.consensus.toFixed(1)}` +
            (onWl ? ' · already on watchlist' : '')
          return (
            <button
              key={p.symbol}
              type="button"
              onClick={() => onPickSymbol(p)}
              title={tooltip}
              className={`flex items-center gap-1 rounded px-2 py-1 text-[11px] transition-colors ${
                isSel
                  ? 'bg-emerald-700 text-emerald-50 ring-2 ring-emerald-400'
                  : 'bg-emerald-900/60 text-emerald-100 hover:bg-emerald-800/70'
              }`}
            >
              <span className="text-[9px] tabular-nums text-emerald-400/80">
                #{i + 1}
              </span>
              <span
                className={`inline-block w-4 rounded text-center text-[9px] font-bold ${tierClr}`}
              >
                {p.bestTier ?? '–'}
              </span>
              <span className="font-semibold">{p.symbol}</span>
              <span className="tabular-nums text-emerald-300">
                {p.avgScore.toFixed(1)}
              </span>
              <span className="text-[9px] text-emerald-400/80">
                {p.cadences.length}/4
              </span>
              {onWl && (
                <span className="text-[9px] text-emerald-300/70" title="already on watchlist">
                  ★
                </span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function FilteredPanel({
  filtered,
}: {
  filtered: Array<{
    symbol: string
    reasons: string[]
    kind: 'rejected' | 'no-data'
  }>
}) {
  const [expanded, setExpanded] = useState(false)
  // Aggregate reason counts for the collapsed summary
  const counts: Record<string, number> = {}
  for (const f of filtered) {
    for (const r of f.reasons) counts[r] = (counts[r] ?? 0) + 1
  }
  const topReasons = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
  return (
    <div className="shrink-0 rounded border border-neutral-800 bg-neutral-950">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-1 text-left text-[11px] hover:bg-neutral-900"
      >
        <span className="font-semibold uppercase tracking-wide text-neutral-300">
          Filtered · {filtered.length}
        </span>
        <span className="flex items-center gap-2 text-neutral-500">
          <span className="font-mono">
            {topReasons.map(([r, n]) => `${r} ${n}`).join(' · ')}
          </span>
          <span className="text-neutral-600">{expanded ? '▾' : '▸'}</span>
        </span>
      </button>
      {expanded && (
        <div className="max-h-40 overflow-auto border-t border-neutral-800 px-2 py-1">
          <table className="w-full text-[11px]">
            <thead className="text-[9px] uppercase tracking-wide text-neutral-500">
              <tr>
                <th className="p-1 text-left">Symbol</th>
                <th className="p-1 text-left">Why filtered</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((f) => (
                <tr key={f.symbol} className="border-b border-neutral-900">
                  <td className="p-1 font-semibold">{f.symbol}</td>
                  <td className="p-1 font-mono text-neutral-400">
                    {f.kind === 'no-data' ? (
                      <span className="text-neutral-600">
                        no provider data (delisted, ticker change, or fetch failed)
                      </span>
                    ) : (
                      f.reasons.join(' · ')
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function cadenceLabel(seconds: number): string {
  const opt = CADENCE_OPTIONS.find((o) => o.seconds === seconds)
  return opt ? opt.label : `${seconds}s`
}

/** Cadence values are seconds (60/120/300/900); the chart's `Interval`
 * type is a string (`'1m' | '2m' | '5m' | '15m'`). They map 1:1 — both
 * follow the spec's PER_*_MINUTE_SCORING.md docs. */
function cadenceToInterval(seconds: number): '1m' | '2m' | '5m' | '15m' {
  if (seconds <= 60) return '1m'
  if (seconds <= 120) return '2m'
  if (seconds <= 300) return '5m'
  return '15m'
}

function SourceChip({
  active,
  onClick,
  label,
}: {
  active: boolean
  onClick: () => void
  label: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded px-2 py-1 text-[11px] font-semibold uppercase tracking-wide transition-colors ${
        active
          ? 'bg-neutral-700 text-neutral-100'
          : 'bg-neutral-900 text-neutral-500 hover:text-neutral-300'
      }`}
    >
      {label}
    </button>
  )
}
