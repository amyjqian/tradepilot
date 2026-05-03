import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createChart,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LineData,
  type UTCTimestamp,
} from 'lightweight-charts'
import { BASE_URL } from '../api'

interface Bar {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

type Interval = '1m' | '2m' | '5m' | '15m' | '1d'

interface Props {
  ticker: string
  /** Initial interval; user can change via the toolbar. Persisted per ticker. */
  defaultInterval?: Interval
  lookbackDays?: number
}

type StreamEvent =
  | { kind: 'bar'; payload: Bar & { vwap?: number } }
  | { kind: 'trade'; payload: { price: number; size: number; ts: number } }

const INTERVAL_SECONDS: Record<Interval, number> = {
  '1m': 60,
  '2m': 120,
  '5m': 300,
  '15m': 900,
  '1d': 86400,
}

const INTERVAL_DEFAULT_LOOKBACK: Record<Interval, number> = {
  '1m': 2,
  '2m': 3,
  '5m': 5,
  '15m': 10,
  '1d': 90,
}

/** Initial visible-bar count per interval — gives the chart a useful
 * "zoom" on first paint instead of fitting all history. The user can
 * still scroll/pinch, the right edge stays anchored as new bars arrive
 * (lightweight-charts default), and switching intervals re-zooms.
 *
 *   1m  → ~4 trading hours    (240 RTH minutes ≈ 60% of a session)
 *   2m  → ~6 hours
 *   5m  → ~1 trading day      (78 RTH bars/day)
 *   15m → ~5 trading days
 *   1d  → ~3 months
 */
const DEFAULT_VISIBLE_BARS: Record<Interval, number> = {
  '1m': 240,
  '2m': 180,
  '5m': 78,
  '15m': 130,
  '1d': 60,
}

const INDICATOR_KEY = 'tradepilot.chart_indicators'
const INTERVAL_KEY_PREFIX = 'tradepilot.chart_interval'

interface IndicatorState {
  ema9: boolean
  vwap: boolean
  rsi9: boolean
}

const DEFAULT_INDICATORS: IndicatorState = {
  ema9: true,
  vwap: true,
  rsi9: false,
}

function loadIndicators(): IndicatorState {
  try {
    const raw = window.localStorage.getItem(INDICATOR_KEY)
    if (!raw) return { ...DEFAULT_INDICATORS }
    const parsed = JSON.parse(raw) as Partial<IndicatorState>
    return {
      ema9: Boolean(parsed.ema9 ?? DEFAULT_INDICATORS.ema9),
      vwap: Boolean(parsed.vwap ?? DEFAULT_INDICATORS.vwap),
      rsi9: Boolean(parsed.rsi9 ?? DEFAULT_INDICATORS.rsi9),
    }
  } catch {
    return { ...DEFAULT_INDICATORS }
  }
}

function loadInterval(ticker: string, fallback: Interval): Interval {
  const raw = window.localStorage.getItem(`${INTERVAL_KEY_PREFIX}.${ticker}`)
  if (raw && (raw === '1m' || raw === '2m' || raw === '5m' || raw === '15m' || raw === '1d')) {
    return raw
  }
  return fallback
}

const ET_DATE_FORMATTER = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  year: 'numeric',
  month: 'short',
  day: 'numeric',
})

const ET_TIME_FORMATTER = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const ET_DATETIME_FORMATTER = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

function etDateKey(epochSec: number): string {
  // YYYY-MM-DD in ET — used as the session boundary for VWAP reset.
  const parts = ET_DATE_FORMATTER.formatToParts(new Date(epochSec * 1000))
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
  // Intl returns months as "Jan", "Feb"… — we just need a stable key.
  return `${get('year')}-${get('month')}-${get('day')}`
}

/** Real-time chart. Uses TradingView's open-source `lightweight-charts`
 * library, fed by our own /quotes endpoints (Polygon REST for history,
 * Polygon WS for live updates). All times displayed in America/New_York
 * regardless of the browser's locale.
 *
 * Polygon's WebSocket only streams 1m aggregates (`AM`). For 2/5/15m
 * we bucket incoming AM events into the higher-TF bar client-side; for
 * 1d we just refresh history every 60 s. */
export function RealtimeChart({
  ticker,
  defaultInterval = '1m',
  lookbackDays,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const ema9SeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const vwapSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const rsiSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const sseRef = useRef<EventSource | null>(null)
  /** Horizontal live-price line on the candle pane. Created lazily on
   * the first price we know, then updated on every trade tick so the
   * line slides with the tape — visible cue of "where price is now"
   * even when the latest candle is mid-formation. */
  const livePriceLineRef = useRef<IPriceLine | null>(null)

  // Cache the bars on the client so live updates can recompute
  // indicators that depend on history (EMA9, VWAP, RSI9).
  const barsRef = useRef<Bar[]>([])
  const sessionVwapRef = useRef<{ key: string; cumPV: number; cumV: number }>({
    key: '',
    cumPV: 0,
    cumV: 0,
  })

  const [interval, setInterval] = useState<Interval>(() =>
    loadInterval(ticker, defaultInterval),
  )
  const [indicators, setIndicators] = useState<IndicatorState>(() => loadIndicators())
  const [error, setError] = useState<string | null>(null)
  const [streamConnected, setStreamConnected] = useState(false)
  /** The most recent bar's close — updates on every live AM event so
   * the toolbar pill ticks within the minute. */
  const [lastPrice, setLastPrice] = useState<number | null>(null)
  /** Yesterday's session close (last bar from the prior ET trading
   * day). Used to compute today's % change vs prior close. Null when
   * the loaded history doesn't reach back into a prior session. */
  const [prevSessionClose, setPrevSessionClose] = useState<number | null>(null)

  // Persist user's interval choice per ticker.
  useEffect(() => {
    window.localStorage.setItem(`${INTERVAL_KEY_PREFIX}.${ticker}`, interval)
  }, [ticker, interval])

  useEffect(() => {
    window.localStorage.setItem(INDICATOR_KEY, JSON.stringify(indicators))
  }, [indicators])

  // 1. Create the chart once on mount.
  useEffect(() => {
    if (!containerRef.current) return
    const isDark =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-color-scheme: dark)').matches
    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: isDark ? '#0a0a0a' : '#ffffff' },
        textColor: isDark ? '#d4d4d4' : '#262626',
      },
      grid: {
        vertLines: { color: isDark ? '#1f1f1f' : '#f5f5f5' },
        horzLines: { color: isDark ? '#1f1f1f' : '#f5f5f5' },
      },
      rightPriceScale: { borderColor: isDark ? '#262626' : '#e5e5e5' },
      timeScale: {
        borderColor: isDark ? '#262626' : '#e5e5e5',
        timeVisible: true,
        secondsVisible: false,
        // Render axis labels in ET regardless of browser locale.
        tickMarkFormatter: (time) => {
          const d = new Date((time as number) * 1000)
          return ET_TIME_FORMATTER.format(d)
        },
      },
      crosshair: { mode: 1 },
      localization: {
        // Crosshair tooltip / price-line label time formatter (also ET).
        timeFormatter: (time) => {
          const d = new Date((time as number) * 1000)
          return ET_DATETIME_FORMATTER.format(d)
        },
      },
      autoSize: true,
    })
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#16a34a',
      borderDownColor: '#dc2626',
      wickUpColor: '#16a34a',
      wickDownColor: '#dc2626',
    })
    const vol = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      color: '#3f3f46',
    })
    chart.priceScale('vol').applyOptions({
      scaleMargins: { top: 0.75, bottom: 0 },
    })
    chartRef.current = chart
    candleSeriesRef.current = candle
    volSeriesRef.current = vol

    return () => {
      chart.remove()
      chartRef.current = null
      candleSeriesRef.current = null
      volSeriesRef.current = null
      ema9SeriesRef.current = null
      vwapSeriesRef.current = null
      rsiSeriesRef.current = null
      livePriceLineRef.current = null
    }
  }, [])

  // 2. Indicator series — created/destroyed when toggles flip.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    if (indicators.ema9 && !ema9SeriesRef.current) {
      ema9SeriesRef.current = chart.addSeries(LineSeries, {
        color: '#fbbf24', // amber
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      })
    } else if (!indicators.ema9 && ema9SeriesRef.current) {
      chart.removeSeries(ema9SeriesRef.current)
      ema9SeriesRef.current = null
    }
    if (indicators.vwap && !vwapSeriesRef.current) {
      vwapSeriesRef.current = chart.addSeries(LineSeries, {
        color: '#a78bfa', // violet
        lineWidth: 1,
        lineStyle: 2, // dashed
        priceLineVisible: false,
        lastValueVisible: false,
      })
    } else if (!indicators.vwap && vwapSeriesRef.current) {
      chart.removeSeries(vwapSeriesRef.current)
      vwapSeriesRef.current = null
    }
    if (indicators.rsi9 && !rsiSeriesRef.current) {
      // RSI lives on its own pane (paneIndex=1) with 0-100 scale.
      rsiSeriesRef.current = chart.addSeries(
        LineSeries,
        {
          color: '#22d3ee', // cyan
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: true,
          priceFormat: { type: 'price', precision: 1, minMove: 0.1 },
        },
        1,
      )
      // Reference lines at 30/70.
      rsiSeriesRef.current.createPriceLine({
        price: 70,
        color: '#52525b',
        lineStyle: 2,
        lineWidth: 1,
        axisLabelVisible: true,
        title: '70',
      })
      rsiSeriesRef.current.createPriceLine({
        price: 30,
        color: '#52525b',
        lineStyle: 2,
        lineWidth: 1,
        axisLabelVisible: true,
        title: '30',
      })
    } else if (!indicators.rsi9 && rsiSeriesRef.current) {
      chart.removeSeries(rsiSeriesRef.current)
      rsiSeriesRef.current = null
    }
    // Recompute indicator data from the cached bars whenever a series
    // is added (so it shows up immediately, not just on the next bar).
    if (barsRef.current.length > 0) {
      drawIndicators(barsRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indicators])

  // 3. Load history + open SSE whenever ticker / interval changes.
  useEffect(() => {
    if (!ticker || !candleSeriesRef.current || !volSeriesRef.current) return
    let cancelled = false
    setError(null)
    setStreamConnected(false)

    const lookback = lookbackDays ?? INTERVAL_DEFAULT_LOOKBACK[interval]
    const controller = new AbortController()
    const url =
      `${BASE_URL}/quotes/history/${encodeURIComponent(ticker)}` +
      `?interval=${interval}&lookback_days=${lookback}`

    fetch(url, { signal: controller.signal })
      .then(async (r) => {
        if (!r.ok) {
          const text = await r.text().catch(() => '')
          throw new Error(`history ${r.status} ${text}`)
        }
        return r.json() as Promise<{ bars: Bar[] }>
      })
      .then((data) => {
        if (cancelled) return
        barsRef.current = data.bars
        const candles: CandlestickData[] = data.bars.map((b) => ({
          time: b.time as UTCTimestamp,
          open: b.open,
          high: b.high,
          low: b.low,
          close: b.close,
        }))
        const vols: HistogramData[] = data.bars.map((b) => ({
          time: b.time as UTCTimestamp,
          value: b.volume,
          color:
            b.close >= b.open
              ? 'rgba(34,197,94,0.4)'
              : 'rgba(239,68,68,0.4)',
        }))
        candleSeriesRef.current?.setData(candles)
        volSeriesRef.current?.setData(vols)
        drawIndicators(data.bars)

        // Latest price + prior-session close for the toolbar pill +
        // ghost line. Trade events will keep both ticking sub-second
        // once the WS auths.
        const last = data.bars[data.bars.length - 1] ?? null
        setLastPrice(last?.close ?? null)
        setPrevSessionClose(findPrevSessionClose(data.bars))
        if (last) setLivePrice(last.close)

        // Initial zoom — show the last N bars per interval rather than
        // fitting all history. The right edge stays anchored as new
        // bars arrive (lightweight-charts default), so the user keeps
        // a "live" window without re-snapping after every tick.
        const totalBars = data.bars.length
        if (totalBars > 0) {
          const visibleBars = DEFAULT_VISIBLE_BARS[interval]
          chartRef.current?.timeScale().setVisibleLogicalRange({
            from: Math.max(0, totalBars - visibleBars),
            to: totalBars,
          })
        }
      })
      .catch((e) => {
        if (cancelled) return
        if ((e as Error).name === 'AbortError') return
        setError(`history: ${String(e)}`)
      })

    // Live updates — Polygon AM (1-minute) events through our SSE.
    // For non-1m intervals we bucket the events into the higher TF.
    const es = new EventSource(
      `${BASE_URL}/quotes/stream/${encodeURIComponent(ticker)}`,
    )
    sseRef.current = es

    es.addEventListener('connected', () => {
      if (cancelled) return
      setStreamConnected(true)
    })
    es.onmessage = (ev) => {
      if (cancelled) return
      try {
        const msg = JSON.parse(ev.data) as StreamEvent
        if (msg.kind === 'bar') {
          applyLiveBar(msg.payload)
        } else if (msg.kind === 'trade') {
          // Sub-second tick — update the toolbar pill + live-price
          // line. The candle stays minute-cadence; this is purely the
          // "ongoing trend" cue between AM events.
          setLastPrice(msg.payload.price)
          setLivePrice(msg.payload.price)
        }
      } catch (e) {
        setError(`stream parse: ${String(e)}`)
      }
    }
    es.onerror = () => setStreamConnected(false)

    return () => {
      cancelled = true
      controller.abort()
      es.close()
      sseRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticker, interval, lookbackDays])

  /** Upsert the live-price horizontal line at the given price. Color
   * follows the move vs prior session's close so the visual matches
   * the toolbar pill. Created lazily on the first known price. */
  function setLivePrice(price: number): void {
    const candle = candleSeriesRef.current
    if (!candle) return
    const prev = prevSessionClose
    const positive = prev === null ? true : price >= prev
    const color = positive ? '#22c55e' : '#ef4444'
    if (livePriceLineRef.current) {
      livePriceLineRef.current.applyOptions({ price, color })
    } else {
      livePriceLineRef.current = candle.createPriceLine({
        price,
        color,
        lineWidth: 1,
        lineStyle: 0, // solid
        lineVisible: true,
        axisLabelVisible: true,
        title: '',
      })
    }
  }

  /** Bucket a 1-minute Polygon AM event into the active interval's bar
   * and update the chart. For 1m it's an in-place replacement; for
   * 2m/5m/15m we extend the latest bar's high/low/close + volume.
   * For 1d we update the day's bar similarly. */
  function applyLiveBar(b: Bar): void {
    const intervalSec = INTERVAL_SECONDS[interval]
    const bucketStart = Math.floor(b.time / intervalSec) * intervalSec

    const bars = barsRef.current
    const last = bars[bars.length - 1]

    let updated: Bar
    if (last && last.time === bucketStart) {
      // Update the existing bar in place.
      updated = {
        time: bucketStart,
        open: last.open,
        high: Math.max(last.high, b.high),
        low: Math.min(last.low, b.low),
        close: b.close,
        volume: last.volume + b.volume,
      }
      bars[bars.length - 1] = updated
    } else if (last && bucketStart < last.time) {
      // Out-of-order or backfill — ignore.
      return
    } else {
      // New bucket.
      updated = { ...b, time: bucketStart }
      bars.push(updated)
    }

    candleSeriesRef.current?.update({
      time: updated.time as UTCTimestamp,
      open: updated.open,
      high: updated.high,
      low: updated.low,
      close: updated.close,
    })
    volSeriesRef.current?.update({
      time: updated.time as UTCTimestamp,
      value: updated.volume,
      color:
        updated.close >= updated.open
          ? 'rgba(34,197,94,0.4)'
          : 'rgba(239,68,68,0.4)',
    })
    setLastPrice(updated.close)
    // Also drive the live-price line from the candle close so plans
    // without `T` entitlement still see a moving line on every minute.
    setLivePrice(updated.close)

    // Indicators that depend on the latest bar.
    if (indicators.ema9 && ema9SeriesRef.current) {
      const ema = computeLatestEma(bars, 9)
      if (ema !== null) {
        ema9SeriesRef.current.update({
          time: updated.time as UTCTimestamp,
          value: ema,
        })
      }
    }
    if (indicators.vwap && vwapSeriesRef.current) {
      const vwap = updateSessionVwap(updated)
      if (vwap !== null) {
        vwapSeriesRef.current.update({
          time: updated.time as UTCTimestamp,
          value: vwap,
        })
      }
    }
    if (indicators.rsi9 && rsiSeriesRef.current) {
      const rsi = computeLatestRsi(bars, 9)
      if (rsi !== null) {
        rsiSeriesRef.current.update({
          time: updated.time as UTCTimestamp,
          value: rsi,
        })
      }
    }
  }

  /** Recompute and set every active indicator from a bar history. Called
   * after history load and whenever an indicator is toggled on. */
  function drawIndicators(bars: Bar[]): void {
    if (indicators.ema9 && ema9SeriesRef.current) {
      ema9SeriesRef.current.setData(computeEmaSeries(bars, 9))
    }
    if (indicators.vwap && vwapSeriesRef.current) {
      vwapSeriesRef.current.setData(computeSessionVwapSeries(bars))
      // Reset the rolling session state so live updates start where the
      // history's last session left off.
      const last = bars[bars.length - 1]
      if (last) {
        const key = etDateKey(last.time)
        sessionVwapRef.current = recomputeSessionStateForKey(bars, key)
      }
    }
    if (indicators.rsi9 && rsiSeriesRef.current) {
      rsiSeriesRef.current.setData(computeRsiSeries(bars, 9))
    }
  }

  function updateSessionVwap(latest: Bar): number | null {
    const key = etDateKey(latest.time)
    const tp = (latest.high + latest.low + latest.close) / 3
    const cur = sessionVwapRef.current
    if (cur.key !== key) {
      // New session — reset.
      sessionVwapRef.current = {
        key,
        cumPV: tp * latest.volume,
        cumV: latest.volume,
      }
    } else {
      // Same session — extend cumulative totals. But if this bar was an
      // update of an already-counted bucket, we'd be double-counting.
      // We avoid that by recomputing from scratch on each live tick —
      // the bar history is small enough that this is cheap (<1ms for
      // a couple hundred bars).
      sessionVwapRef.current = recomputeSessionStateForKey(barsRef.current, key)
    }
    const s = sessionVwapRef.current
    return s.cumV > 0 ? s.cumPV / s.cumV : null
  }

  // Toolbar
  const intervals: Interval[] = useMemo(() => ['1m', '2m', '5m', '15m', '1d'], [])

  return (
    <div className="relative flex h-full w-full flex-col">
      <div className="flex flex-wrap items-center gap-1 border-b border-neutral-800 px-1 py-1 text-[10px]">
        <span className="uppercase tracking-wide text-neutral-500">TF</span>
        {intervals.map((iv) => (
          <button
            key={iv}
            type="button"
            onClick={() => setInterval(iv)}
            className={`rounded border px-1.5 py-0.5 font-medium transition-colors ${
              interval === iv
                ? 'border-[var(--color-accent)] bg-[var(--color-accent)]/20 text-[var(--color-accent)]'
                : 'border-neutral-700 text-neutral-400 hover:border-neutral-500 hover:text-neutral-200'
            }`}
          >
            {iv}
          </button>
        ))}
        <span className="ml-2 uppercase tracking-wide text-neutral-500">Indicators</span>
        <IndicatorToggle
          label="EMA9"
          color="#fbbf24"
          on={indicators.ema9}
          onClick={() => setIndicators((p) => ({ ...p, ema9: !p.ema9 }))}
        />
        <IndicatorToggle
          label="VWAP"
          color="#a78bfa"
          on={indicators.vwap}
          onClick={() => setIndicators((p) => ({ ...p, vwap: !p.vwap }))}
        />
        <IndicatorToggle
          label="RSI9"
          color="#22d3ee"
          on={indicators.rsi9}
          onClick={() => setIndicators((p) => ({ ...p, rsi9: !p.rsi9 }))}
        />
        <span className="ml-auto flex items-center gap-2">
          <LastPricePill last={lastPrice} prevClose={prevSessionClose} />
          <span className="flex items-center gap-1 text-neutral-500">
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                streamConnected ? 'bg-[var(--color-accent)] animate-pulse' : 'bg-neutral-600'
              }`}
            />
            <span className="uppercase tracking-wide">
              {streamConnected ? 'live · ET' : error ? 'offline · ET' : 'connecting · ET'}
            </span>
          </span>
        </span>
      </div>
      <div ref={containerRef} className="min-h-0 flex-1" />
      {error && (
        <div className="absolute bottom-2 left-2 rounded bg-red-950/80 px-2 py-1 text-[10px] text-[var(--color-danger)]">
          {error}
        </div>
      )}
    </div>
  )
}

function LastPricePill({
  last,
  prevClose,
}: {
  last: number | null
  prevClose: number | null
}) {
  if (last === null) return null
  const diff = prevClose !== null ? last - prevClose : null
  const pct = prevClose !== null && prevClose > 0 ? (diff! / prevClose) * 100 : null
  const positive = diff !== null && diff >= 0
  const color =
    diff === null
      ? 'text-neutral-200'
      : positive
        ? 'text-[var(--color-accent-dim)]'
        : 'text-[var(--color-danger)]'
  return (
    <span
      className="flex items-baseline gap-1.5 rounded border border-neutral-800 bg-neutral-900/60 px-2 py-0.5"
      title="Last price (updates every minute) and today's % vs prior session close"
    >
      <span className={`num text-xs font-semibold ${color}`}>
        ${last.toFixed(2)}
      </span>
      {pct !== null && (
        <span className={`num text-[10px] ${color}`}>
          {positive ? '+' : ''}
          {pct.toFixed(2)}%
        </span>
      )}
    </span>
  )
}

/** Find the close of the last bar in the prior ET trading day. Returns
 * null when the loaded history all falls within today's session. */
function findPrevSessionClose(bars: Bar[]): number | null {
  if (bars.length < 2) return null
  const lastKey = etDateKey(bars[bars.length - 1].time)
  for (let i = bars.length - 2; i >= 0; i--) {
    if (etDateKey(bars[i].time) !== lastKey) return bars[i].close
  }
  return null
}

function IndicatorToggle({
  label,
  color,
  on,
  onClick,
}: {
  label: string
  color: string
  on: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={`${on ? 'Hide' : 'Show'} ${label}`}
      className={`flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
        on
          ? 'border-neutral-600 text-neutral-200'
          : 'border-neutral-800 text-neutral-500 hover:border-neutral-600 hover:text-neutral-300'
      }`}
    >
      <span
        className="inline-block h-1.5 w-3 rounded-sm"
        style={{ backgroundColor: on ? color : 'transparent', borderBottom: on ? 'none' : `1px dashed ${color}` }}
      />
      {label}
    </button>
  )
}

// ----------------------------------------------------------------------
// Indicator math
// ----------------------------------------------------------------------

function computeEmaSeries(bars: Bar[], period: number): LineData[] {
  if (bars.length === 0) return []
  const alpha = 2 / (period + 1)
  const out: LineData[] = []
  let ema = bars[0].close
  for (let i = 0; i < bars.length; i++) {
    const c = bars[i].close
    ema = i === 0 ? c : ema + alpha * (c - ema)
    out.push({ time: bars[i].time as UTCTimestamp, value: ema })
  }
  return out
}

function computeLatestEma(bars: Bar[], period: number): number | null {
  if (bars.length === 0) return null
  const alpha = 2 / (period + 1)
  let ema = bars[0].close
  for (let i = 1; i < bars.length; i++) {
    ema += alpha * (bars[i].close - ema)
  }
  return ema
}

function computeSessionVwapSeries(bars: Bar[]): LineData[] {
  const out: LineData[] = []
  let cumPV = 0
  let cumV = 0
  let curKey = ''
  for (const b of bars) {
    const k = etDateKey(b.time)
    if (k !== curKey) {
      curKey = k
      cumPV = 0
      cumV = 0
    }
    const tp = (b.high + b.low + b.close) / 3
    cumPV += tp * b.volume
    cumV += b.volume
    if (cumV > 0) {
      out.push({ time: b.time as UTCTimestamp, value: cumPV / cumV })
    }
  }
  return out
}

function recomputeSessionStateForKey(
  bars: Bar[],
  key: string,
): { key: string; cumPV: number; cumV: number } {
  let cumPV = 0
  let cumV = 0
  for (const b of bars) {
    if (etDateKey(b.time) !== key) continue
    const tp = (b.high + b.low + b.close) / 3
    cumPV += tp * b.volume
    cumV += b.volume
  }
  return { key, cumPV, cumV }
}

function computeRsiSeries(bars: Bar[], period: number): LineData[] {
  // Wilder's RSI: smoothed average of gains / losses over `period`.
  if (bars.length <= period) return []
  const out: LineData[] = []
  let gainAvg = 0
  let lossAvg = 0
  // Seed averages from the first `period` deltas.
  for (let i = 1; i <= period; i++) {
    const delta = bars[i].close - bars[i - 1].close
    if (delta >= 0) gainAvg += delta
    else lossAvg += -delta
  }
  gainAvg /= period
  lossAvg /= period
  let rsi = lossAvg === 0 ? 100 : 100 - 100 / (1 + gainAvg / lossAvg)
  out.push({ time: bars[period].time as UTCTimestamp, value: rsi })
  for (let i = period + 1; i < bars.length; i++) {
    const delta = bars[i].close - bars[i - 1].close
    const gain = delta > 0 ? delta : 0
    const loss = delta < 0 ? -delta : 0
    gainAvg = (gainAvg * (period - 1) + gain) / period
    lossAvg = (lossAvg * (period - 1) + loss) / period
    rsi = lossAvg === 0 ? 100 : 100 - 100 / (1 + gainAvg / lossAvg)
    out.push({ time: bars[i].time as UTCTimestamp, value: rsi })
  }
  return out
}

function computeLatestRsi(bars: Bar[], period: number): number | null {
  const series = computeRsiSeries(bars, period)
  if (series.length === 0) return null
  return series[series.length - 1].value as number
}
