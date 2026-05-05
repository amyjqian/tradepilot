import { useRealtimeQuote } from '../useRealtimeQuote'
import { fmtPct } from '../format'

interface Props {
  ticker: string
  fallbackPct: number
}

// Inline live-pct badge used in the watchlist & sector rotation rows. Matches
// the previous static badge's styling so the layout doesn't shift; dims when
// no live tick has arrived (no Polygon WS entitlement, or pre-market lull) so
// the user can tell whether they're looking at scan-time data or live data.
export function LiveChangeBadge({ ticker, fallbackPct }: Props) {
  const q = useRealtimeQuote(ticker)
  const isLive = q?.connected === true && q?.change_pct != null
  const value = isLive ? (q!.change_pct as number) : fallbackPct
  return (
    <span
      className={`num text-[11px] ${
        value >= 0 ? 'text-[var(--color-accent-dim)]' : 'text-[var(--color-danger)]'
      } ${isLive ? '' : 'opacity-60'}`}
      title={isLive ? 'Live' : 'Last scan value (no live tick yet)'}
    >
      {fmtPct(value, true)}
    </span>
  )
}
