import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ScanResult } from './types'

const STORAGE_KEY = 'tradepilot.result_filters'

export type TrendFilter = 'vwap' | 'ema9' | 'stacked'

/** "Near 20-bar high" filter threshold — a result passes if its
 * `dist_from_20d_high_pct` is less than or equal to this. 2% strikes
 * a balance between strict breakout proximity and capturing setups
 * that are still near the high but pulled back slightly. */
export const NEAR_HIGH_THRESHOLD_PCT = 2.0

const SCORE_DEFAULT = 0
const RSI_MIN_DEFAULT = 0
const RSI_MAX_DEFAULT = 100

export interface FilterState {
  trend: Set<TrendFilter>
  greenDay: boolean
  nearHigh: boolean
  minScore: number
  rsiMin: number
  rsiMax: number
}

interface UseResultFilters {
  state: FilterState
  /** Convenience: true iff any filter is non-default. */
  hasActive: boolean
  /** Number of active filters — used to size the count pill in the UI. */
  activeCount: number
  toggleTrend: (f: TrendFilter) => void
  toggleGreen: () => void
  toggleNearHigh: () => void
  setMinScore: (n: number) => void
  setRsiMin: (n: number) => void
  setRsiMax: (n: number) => void
  clear: () => void
  /** Apply all active filters to a list of scan results. */
  apply: <T extends ScanResult>(results: T[]) => T[]
}

const TREND_VALID: ReadonlySet<string> = new Set(['vwap', 'ema9', 'stacked'])

const DEFAULT_STATE: FilterState = {
  trend: new Set(),
  greenDay: false,
  nearHigh: false,
  minScore: SCORE_DEFAULT,
  rsiMin: RSI_MIN_DEFAULT,
  rsiMax: RSI_MAX_DEFAULT,
}

interface StoredState {
  trend?: string[]
  greenDay?: boolean
  nearHigh?: boolean
  minScore?: number
  rsiMin?: number
  rsiMax?: number
}

function loadStored(): FilterState {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return cloneDefault()
    const parsed = JSON.parse(raw) as StoredState
    return {
      trend: new Set(
        (parsed.trend ?? []).filter(
          (f): f is TrendFilter => typeof f === 'string' && TREND_VALID.has(f),
        ),
      ),
      greenDay: Boolean(parsed.greenDay),
      nearHigh: Boolean(parsed.nearHigh),
      minScore: clampNumber(parsed.minScore, 0, 100, SCORE_DEFAULT),
      rsiMin: clampNumber(parsed.rsiMin, 0, 100, RSI_MIN_DEFAULT),
      rsiMax: clampNumber(parsed.rsiMax, 0, 100, RSI_MAX_DEFAULT),
    }
  } catch {
    return cloneDefault()
  }
}

function clampNumber(
  n: unknown,
  min: number,
  max: number,
  fallback: number,
): number {
  if (typeof n !== 'number' || !Number.isFinite(n)) return fallback
  return Math.min(max, Math.max(min, n))
}

function cloneDefault(): FilterState {
  return { ...DEFAULT_STATE, trend: new Set() }
}

function persist(state: FilterState): void {
  const payload: StoredState = {
    trend: Array.from(state.trend),
    greenDay: state.greenDay,
    nearHigh: state.nearHigh,
    minScore: state.minScore,
    rsiMin: state.rsiMin,
    rsiMax: state.rsiMax,
  }
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
}

/** Client-side post-scan filter for trend, momentum, and quality
 * conditions. Active filters are AND-combined; turning each one off
 * has no effect (i.e. defaults are pass-through). Persists across
 * sessions in localStorage. */
export function useResultFilters(): UseResultFilters {
  const [state, setState] = useState<FilterState>(() => loadStored())

  useEffect(() => {
    persist(state)
  }, [state])

  const toggleTrend = useCallback((f: TrendFilter) => {
    setState((prev) => {
      const next = new Set(prev.trend)
      if (next.has(f)) next.delete(f)
      else next.add(f)
      return { ...prev, trend: next }
    })
  }, [])

  const toggleGreen = useCallback(() => {
    setState((prev) => ({ ...prev, greenDay: !prev.greenDay }))
  }, [])

  const toggleNearHigh = useCallback(() => {
    setState((prev) => ({ ...prev, nearHigh: !prev.nearHigh }))
  }, [])

  const setMinScore = useCallback((n: number) => {
    setState((prev) => ({
      ...prev,
      minScore: clampNumber(n, 0, 100, SCORE_DEFAULT),
    }))
  }, [])

  const setRsiMin = useCallback((n: number) => {
    setState((prev) => ({
      ...prev,
      rsiMin: clampNumber(n, 0, 100, RSI_MIN_DEFAULT),
    }))
  }, [])

  const setRsiMax = useCallback((n: number) => {
    setState((prev) => ({
      ...prev,
      rsiMax: clampNumber(n, 0, 100, RSI_MAX_DEFAULT),
    }))
  }, [])

  const clear = useCallback(() => {
    setState(cloneDefault())
  }, [])

  const apply = useCallback(
    <T extends ScanResult>(results: T[]): T[] => {
      const s = state
      // Cheap pre-check — if nothing is non-default, return results
      // unchanged so the typical "no filters" path doesn't allocate.
      if (
        s.trend.size === 0 &&
        !s.greenDay &&
        !s.nearHigh &&
        s.minScore === SCORE_DEFAULT &&
        s.rsiMin === RSI_MIN_DEFAULT &&
        s.rsiMax === RSI_MAX_DEFAULT
      ) {
        return results
      }
      return results.filter((r) => {
        if (s.trend.has('vwap') && !r.above_vwap) return false
        if (s.trend.has('ema9') && !r.above_ema9) return false
        if (s.trend.has('stacked') && !r.ema_stacked) return false
        if (s.greenDay && !(r.pct_change > 0)) return false
        if (s.nearHigh && !(r.dist_from_20d_high_pct <= NEAR_HIGH_THRESHOLD_PCT))
          return false
        if (r.score < s.minScore) return false
        if (r.rsi < s.rsiMin || r.rsi > s.rsiMax) return false
        return true
      })
    },
    [state],
  )

  const activeCount = useMemo(() => {
    let n = state.trend.size
    if (state.greenDay) n++
    if (state.nearHigh) n++
    if (state.minScore > SCORE_DEFAULT) n++
    if (state.rsiMin > RSI_MIN_DEFAULT || state.rsiMax < RSI_MAX_DEFAULT) n++
    return n
  }, [state])

  const hasActive = activeCount > 0

  return useMemo(
    () => ({
      state,
      hasActive,
      activeCount,
      toggleTrend,
      toggleGreen,
      toggleNearHigh,
      setMinScore,
      setRsiMin,
      setRsiMax,
      clear,
      apply,
    }),
    [
      state,
      hasActive,
      activeCount,
      toggleTrend,
      toggleGreen,
      toggleNearHigh,
      setMinScore,
      setRsiMin,
      setRsiMax,
      clear,
      apply,
    ],
  )
}
