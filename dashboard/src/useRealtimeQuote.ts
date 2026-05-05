import { useEffect, useState } from 'react'
import { subscribeQuote, type QuoteSnapshot } from './quoteStream'

// React hook around the quoteStream hub. Returns the latest snapshot for a
// ticker (price, prior_close, change_pct, connected state) and re-renders on
// every tick. Pass `null` to disable subscription.
export function useRealtimeQuote(ticker: string | null): QuoteSnapshot | null {
  const [snap, setSnap] = useState<QuoteSnapshot | null>(null)
  useEffect(() => {
    if (!ticker) {
      setSnap(null)
      return
    }
    // Spread so React notices the change — the hub mutates the same object
    // for performance, but components must see a new reference to re-render.
    const unsub = subscribeQuote(ticker, (s) => setSnap({ ...s }))
    return unsub
  }, [ticker])
  return snap
}
