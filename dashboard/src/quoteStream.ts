import { BASE_URL } from './api'

// Browser-side multiplexer for live quotes.
//
// All subscribed tickers share a SINGLE EventSource connected to the
// backend's /quotes/stream-multi endpoint. The previous per-ticker design
// burned one connection per unique watchlist row — once we hit ~6 unique
// tickers the browser's HTTP/1.1 cap kicked in and queued every other
// fetch (including /quotes/history for the chart and /scan), leaving the
// chart wedged on "CONNECTING…".
//
// Subscriptions are debounced: when components rapidly subscribe to a
// fresh batch of tickers (e.g. a new scan re-rendering 20 row badges),
// we wait ~150ms before reopening with the merged set so we don't
// thrash the upstream connection on every render frame.

export interface QuoteSnapshot {
  ticker: string
  price: number | null
  prior_close: number | null
  change_pct: number | null
  connected: boolean
  last_update_at: number | null
}

export type RawStreamEvent =
  | {
      kind: 'bar'
      payload: {
        time: number
        open: number
        high: number
        low: number
        close: number
        volume: number
        vwap?: number
      }
    }
  | { kind: 'trade'; payload: { price: number; size: number; ts: number } }

type Listener = (snap: QuoteSnapshot) => void
type RawListener = (event: RawStreamEvent) => void

interface SymbolEntry {
  ticker: string
  listeners: Set<Listener>
  rawListeners: Set<RawListener>
  snap: QuoteSnapshot
}

const entries = new Map<string, SymbolEntry>()

let currentEs: EventSource | null = null
let currentTickers: string[] = []
let reopenTimer: number | null = null

const REOPEN_DEBOUNCE_MS = 150

function emitSnapshot(entry: SymbolEntry) {
  for (const listener of entry.listeners) listener(entry.snap)
}

function recomputeChangePct(snap: QuoteSnapshot) {
  if (snap.price != null && snap.prior_close != null && snap.prior_close > 0) {
    snap.change_pct = ((snap.price - snap.prior_close) / snap.prior_close) * 100
  }
}

function arraysEqual(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false
  return true
}

function reopenIfNeeded() {
  const wanted = Array.from(entries.keys()).sort()
  if (arraysEqual(wanted, currentTickers)) return

  // Close the previous EventSource before opening a new one. Each entry's
  // `connected` flag is reset until the new stream's `connected` event
  // arrives, so consumers that watch `snap.connected` can dim the badge
  // briefly during the swap rather than show stale data as live.
  if (currentEs) {
    currentEs.close()
    currentEs = null
    for (const entry of entries.values()) {
      if (entry.snap.connected) {
        entry.snap.connected = false
        emitSnapshot(entry)
      }
    }
  }

  currentTickers = wanted
  if (wanted.length === 0) return

  const url =
    `${BASE_URL}/quotes/stream-multi` +
    `?symbols=${encodeURIComponent(wanted.join(','))}`
  const es = new EventSource(url)
  currentEs = es

  es.addEventListener('connected', (ev) => {
    try {
      const data = JSON.parse((ev as MessageEvent).data) as {
        symbols?: string[]
        prior_closes?: Record<string, number>
      }
      const priorMap = data.prior_closes ?? {}
      for (const ticker of data.symbols ?? []) {
        const entry = entries.get(ticker)
        if (!entry) continue
        entry.snap.connected = true
        const pc = priorMap[ticker]
        if (typeof pc === 'number') entry.snap.prior_close = pc
        recomputeChangePct(entry.snap)
        emitSnapshot(entry)
      }
    } catch {
      // Ignore malformed connected payload; the stream still delivers events.
    }
  })

  es.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data) as RawStreamEvent & { symbol?: string }
      const ticker = msg.symbol
      if (!ticker) return
      const entry = entries.get(ticker)
      if (!entry) return

      let nextPrice: number | null = null
      if (msg.kind === 'trade' && typeof msg.payload?.price === 'number') {
        nextPrice = msg.payload.price
      } else if (msg.kind === 'bar' && typeof msg.payload?.close === 'number') {
        nextPrice = msg.payload.close
      }
      if (nextPrice != null) {
        entry.snap.price = nextPrice
        entry.snap.last_update_at = Date.now()
        recomputeChangePct(entry.snap)
        emitSnapshot(entry)
      }
      if (msg.kind === 'bar' || msg.kind === 'trade') {
        for (const rawListener of entry.rawListeners) rawListener(msg)
      }
    } catch {
      // Drop unparsable frames silently.
    }
  }

  es.onerror = () => {
    // EventSource auto-reconnects; flag every entry stale until the new
    // connected event arrives so consumers can dim their UI.
    for (const entry of entries.values()) {
      if (entry.snap.connected) {
        entry.snap.connected = false
        emitSnapshot(entry)
      }
    }
  }
}

function scheduleReopen() {
  if (reopenTimer != null) return
  reopenTimer = window.setTimeout(() => {
    reopenTimer = null
    reopenIfNeeded()
  }, REOPEN_DEBOUNCE_MS)
}

function ensureEntry(ticker: string): SymbolEntry {
  const existing = entries.get(ticker)
  if (existing) return existing
  const entry: SymbolEntry = {
    ticker,
    listeners: new Set(),
    rawListeners: new Set(),
    snap: {
      ticker,
      price: null,
      prior_close: null,
      change_pct: null,
      connected: false,
      last_update_at: null,
    },
  }
  entries.set(ticker, entry)
  scheduleReopen()
  return entry
}

function maybeDropEntry(entry: SymbolEntry) {
  if (entry.listeners.size === 0 && entry.rawListeners.size === 0) {
    entries.delete(entry.ticker)
    scheduleReopen()
  }
}

export function subscribeQuote(
  ticker: string,
  listener: Listener,
): () => void {
  const entry = ensureEntry(ticker)
  entry.listeners.add(listener)
  // Push the current snapshot immediately so a late subscriber sees the
  // already-known prior_close / last price without waiting for the next tick.
  listener(entry.snap)
  return () => {
    entry.listeners.delete(listener)
    maybeDropEntry(entry)
  }
}

// Subscribe to raw bar/trade events. Used by the chart to drive candle and
// live-price updates without opening its own EventSource.
export function subscribeRawEvents(
  ticker: string,
  listener: RawListener,
): () => void {
  const entry = ensureEntry(ticker)
  entry.rawListeners.add(listener)
  return () => {
    entry.rawListeners.delete(listener)
    maybeDropEntry(entry)
  }
}
