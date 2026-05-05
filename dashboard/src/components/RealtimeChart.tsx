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
import { subscribeQuote, subscribeRawEvents } from '../quoteStream'

interface Bar {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

type Interval = '1m' | '2m' | '5m' | '15m' | '30m' | '1h' | '1d'

interface Props {
  ticker: string
  /** Initial interval; user can change via the toolbar. Persisted per ticker. */
  defaultInterval?: Interval
  lookbackDays?: number
}

const INTERVAL_SECONDS: Record<Interval, number> = {
  '1m': 60,
  '2m': 120,
  '5m': 300,
  '15m': 900,
  '30m': 1800,
  '1h': 3600,
  '1d': 86400,
}

// Mirror DEFAULT_LOOKBACK in App.tsx — see the comment there for rationale.
// Chart and scanner pull the same window so EMAs/VWAP overlay the bars the
// scanner ranked off of, without one path doing more warmup than the other.
const INTERVAL_DEFAULT_LOOKBACK: Record<Interval, number> = {
  '1m': 5,
  '2m': 7,
  '5m': 15,
  '15m': 30,
  '30m': 30,
  '1h': 60,
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
  '30m': 80,
  '1h': 60,
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

const VALID_INTERVALS: ReadonlySet<Interval> = new Set([
  '1m',
  '2m',
  '5m',
  '15m',
  '30m',
  '1h',
  '1d',
])

function loadInterval(ticker: string, fallback: Interval): Interval {
  const raw = window.localStorage.getItem(`${INTERVAL_KEY_PREFIX}.${ticker}`)
  if (raw && VALID_INTERVALS.has(raw as Interval)) return raw as Interval
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
  // Bucket time of the latest bar when it was synthesized from `T` (trade)
  // events rather than confirmed by `AM`. Polygon emits AM at minute close,
  // so without this synthesis the rightmost candle on the chart always
  // represents the *previous* minute — feels like a 1-minute lag. Trade
  // ticks let us draw a live partial candle that grows in real time, then
  // gets replaced by the canonical AM aggregate when the minute rolls over.
  // Only tracked for the 1m timeframe; higher TFs let AM extend the bar.
  const partialBucketRef = useRef<number | null>(null)

  // Lazy-load history when the user pans past the leftmost loaded bar.
  // `loadingMoreRef` gates concurrent fetches; `hasMoreHistoryRef` flips to
  // false when a fetch returns 0 bars (we've hit the ticker's listing date
  // or whatever Polygon will give us). `loadGenRef` is a generation counter
  // bumped on every ticker/interval change so a slow in-flight page-back
  // can't stomp the chart with the previous ticker's bars.
  const loadingMoreRef = useRef<boolean>(false)
  const hasMoreHistoryRef = useRef<boolean>(true)
  const loadGenRef = useRef<number>(0)

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

    // Wipe everything left over from the previously-selected ticker BEFORE
    // the new fetch starts. Without this, the WS subscription for the new
    // ticker begins delivering trade ticks immediately (so the live-price
    // line jumps to the new price) while the candle/EMA9/VWAP series still
    // hold the old ticker's bars. The right price scale then auto-fits over
    // the union of both — e.g. switching from a $14 ticker to WDC at $441
    // leaves the scale stretched ~$14–$441 (showing as labels 100/200/.../500)
    // instead of the tight ~$440–$445 range the new data alone would warrant.
    // Removing the live-price line too prevents its old axis label from
    // ghosting at the previous ticker's price until a fresh tick arrives.
    barsRef.current = []
    sessionVwapRef.current = { key: '', cumPV: 0, cumV: 0 }
    partialBucketRef.current = null
    // Bump the generation so any older lazy-load page-backs in flight
    // (still under the previous ticker) get rejected when they resolve.
    loadGenRef.current += 1
    loadingMoreRef.current = false
    hasMoreHistoryRef.current = true
    setLastPrice(null)
    setPrevSessionClose(null)
    candleSeriesRef.current.setData([])
    volSeriesRef.current.setData([])
    ema9SeriesRef.current?.setData([])
    vwapSeriesRef.current?.setData([])
    rsiSeriesRef.current?.setData([])
    if (livePriceLineRef.current) {
      candleSeriesRef.current.removePriceLine(livePriceLineRef.current)
      livePriceLineRef.current = null
    }
    // Re-arm autoScale in case the user dragged the axis into manual mode
    // on a previous ticker — we want the new bars to drive the visible range.
    chartRef.current?.priceScale('right').applyOptions({ autoScale: true })

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

    // Live updates flow through the shared quoteStream hub so the chart
    // shares an EventSource with the watchlist row badge + the price header
    // for the same ticker. Without this, each chart pane would open its own
    // SSE (one more per ticker) and we'd hit the browser's HTTP/1.1 cap of
    // ~6 connections per origin once the watchlist had a few rows + an open
    // chart, leaving the chart's stream wedged in CONNECTING.
    const unsubSnap = subscribeQuote(ticker, (snap) => {
      if (cancelled) return
      setStreamConnected(snap.connected)
    })
    const unsubRaw = subscribeRawEvents(ticker, (msg) => {
      if (cancelled) return
      if (msg.kind === 'bar') {
        applyLiveBar(msg.payload)
      } else if (msg.kind === 'trade') {
        // Sub-second tick — update the toolbar pill + live-price line and,
        // on the 1m timeframe, grow the partial in-progress candle so the
        // chart isn't stuck on the previous minute's close.
        setLastPrice(msg.payload.price)
        setLivePrice(msg.payload.price)
        applyLiveTrade(msg.payload.price, msg.payload.ts)
      }
    })

    // Lazy-load older bars when the user pans past the leftmost loaded bar.
    // Lightweight-charts' visible-range fires with negative `from` indices
    // when the user has scrolled into "empty space" past the first bar.
    // Trigger when within ~50 bars of the left edge (gives the fetch a
    // chance to complete before the user actually hits the wall).
    const tsApi = chartRef.current?.timeScale()
    const PAGE_TRIGGER_DISTANCE = 50
    const onVisibleRangeChange = (range: { from: number; to: number } | null) => {
      if (!range) return
      if (range.from > PAGE_TRIGGER_DISTANCE) return
      void loadOlderHistory()
    }
    tsApi?.subscribeVisibleLogicalRangeChange(onVisibleRangeChange)

    return () => {
      cancelled = true
      controller.abort()
      unsubSnap()
      unsubRaw()
      tsApi?.unsubscribeVisibleLogicalRangeChange(onVisibleRangeChange)
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
      if (partialBucketRef.current === bucketStart) {
        // Bar was synthesized from trade ticks; replace it with the
        // canonical AM aggregate so OHLCV matches Polygon's official
        // numbers. Without this we'd carry a partial open derived from
        // the first trade we saw and double-count volume on AM arrival.
        updated = { ...b, time: bucketStart }
        partialBucketRef.current = null
      } else {
        // Higher-TF bucket still being filled by successive AM slices.
        updated = {
          time: bucketStart,
          open: last.open,
          high: Math.max(last.high, b.high),
          low: Math.min(last.low, b.low),
          close: b.close,
          volume: last.volume + b.volume,
        }
      }
      bars[bars.length - 1] = updated
    } else if (last && bucketStart < last.time) {
      // Out-of-order or backfill — ignore.
      return
    } else {
      // New bucket.
      updated = { ...b, time: bucketStart }
      bars.push(updated)
      partialBucketRef.current = null
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

  /** Grow the in-progress 1m candle from a sub-second trade tick so the
   * rightmost bar reflects the live tape rather than the previous minute's
   * close. Restricted to 1m: on higher TFs each AM is a slice of an
   * unfinished bucket and combining trade-driven OHLC with AM extensions
   * means double-counting (the AM volume already includes those trades).
   * For 1m, when the minute closes Polygon's AM event will replace this
   * partial with the canonical aggregate via applyLiveBar's partial branch. */
  function applyLiveTrade(price: number, ts: number): void {
    if (interval !== '1m') return
    const intervalSec = INTERVAL_SECONDS[interval]
    const bucketStart = Math.floor(ts / intervalSec) * intervalSec
    const bars = barsRef.current
    const last = bars[bars.length - 1]

    let updated: Bar
    if (last && last.time === bucketStart) {
      // Extend the live partial. close = latest trade; high/low track the
      // running extremes within the minute.
      updated = {
        time: last.time,
        open: last.open,
        high: Math.max(last.high, price),
        low: Math.min(last.low, price),
        close: price,
        volume: last.volume,
      }
      bars[bars.length - 1] = updated
    } else if (last && bucketStart < last.time) {
      return
    } else {
      // First trade of a new minute — open a partial bar at this price.
      // Volume stays 0 until AM provides the canonical figure: we don't
      // see every print due to server-side throttling, so summing trade
      // sizes here would systematically underreport.
      updated = {
        time: bucketStart,
        open: price,
        high: price,
        low: price,
        close: price,
        volume: 0,
      }
      bars.push(updated)
      partialBucketRef.current = bucketStart
    }

    candleSeriesRef.current?.update({
      time: updated.time as UTCTimestamp,
      open: updated.open,
      high: updated.high,
      low: updated.low,
      close: updated.close,
    })
  }

  /** Page in older bars when the user scrolls past the leftmost loaded bar.
   *
   * The chart caches its bars in `barsRef.current`. When the visible range's
   * left edge approaches index 0 (or goes negative — lightweight-charts
   * lets you pan into "empty" space past the first bar), we ask the API
   * for the slice ending just before our current oldest bar. New bars are
   * prepended; the indicators are recomputed; the visible range is shifted
   * by the number of inserted bars so the user's scroll position over the
   * data they were looking at stays put.
   *
   * Stops paging when the API returns 0 bars (we've hit Polygon's available
   * history for this ticker / interval). Concurrent calls are gated by
   * `loadingMoreRef`. A bumped `loadGenRef` cancels any in-flight page-back
   * after the user switches ticker / interval so we don't splice the wrong
   * ticker's bars into the new chart. */
  async function loadOlderHistory(): Promise<void> {
    if (loadingMoreRef.current) return
    if (!hasMoreHistoryRef.current) return
    if (barsRef.current.length === 0) return
    if (!candleSeriesRef.current || !volSeriesRef.current) return

    loadingMoreRef.current = true
    const myGen = loadGenRef.current
    const oldestTime = barsRef.current[0].time
    // Match the chart's mount-time lookback so a 1m chart pages 5 days at
    // a time, 1d pages 90 days at a time, etc. Each fetch is comparable in
    // cost to the initial render — cached parquet path on Polygon means
    // only the new days actually hit the wire.
    const pageDays = INTERVAL_DEFAULT_LOOKBACK[interval]
    const url =
      `${BASE_URL}/quotes/history/${encodeURIComponent(ticker)}` +
      `?interval=${interval}&lookback_days=${pageDays}` +
      `&before_ts=${oldestTime}`

    try {
      const r = await fetch(url)
      if (!r.ok) return
      const data = (await r.json()) as { bars: Bar[] }
      if (myGen !== loadGenRef.current) return
      const incoming = data.bars ?? []
      if (incoming.length === 0) {
        hasMoreHistoryRef.current = false
        return
      }

      // Dedup by `time` — Polygon's date window is inclusive, so the boundary
      // bar between pages would otherwise double up.
      const existingTimes = new Set(barsRef.current.map((b) => b.time))
      const olderOnly = incoming.filter(
        (b) => !existingTimes.has(b.time) && b.time < oldestTime,
      )
      if (olderOnly.length === 0) {
        // Nothing genuinely older arrived — assume we're at the bottom so a
        // user dragging at the edge stops re-firing the same fetch forever.
        hasMoreHistoryRef.current = false
        return
      }

      const merged = [...olderOnly, ...barsRef.current]
      barsRef.current = merged

      // Save the user's current scroll position so we can restore it after
      // setData (which would otherwise auto-fit). Logical indices shift by
      // the number of bars we prepended.
      const tsApi = chartRef.current?.timeScale()
      const savedRange = tsApi?.getVisibleLogicalRange()

      const candles: CandlestickData[] = merged.map((b) => ({
        time: b.time as UTCTimestamp,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      }))
      const vols: HistogramData[] = merged.map((b) => ({
        time: b.time as UTCTimestamp,
        value: b.volume,
        color:
          b.close >= b.open
            ? 'rgba(34,197,94,0.4)'
            : 'rgba(239,68,68,0.4)',
      }))
      candleSeriesRef.current.setData(candles)
      volSeriesRef.current.setData(vols)
      drawIndicators(merged)

      const shift = olderOnly.length
      if (savedRange && tsApi) {
        tsApi.setVisibleLogicalRange({
          from: savedRange.from + shift,
          to: savedRange.to + shift,
        })
      }
    } catch {
      // Network/parse failures aren't fatal — the user can pan again and
      // we'll retry. We don't surface an error toast for these because the
      // chart still has the data it had before.
    } finally {
      loadingMoreRef.current = false
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
  const intervals: Interval[] = useMemo(
    () => ['1m', '2m', '5m', '15m', '30m', '1h', '1d'],
    [],
  )

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
