import { useState } from 'react'
import type { ScanResult } from '../types'
import { SectorRotationPanel } from './SectorRotationPanel'
import { WatchlistPanel } from './WatchlistPanel'
import { BacktestPanel } from './BacktestPanel'
import { JournalPanel } from './JournalPanel'

type Tab = 'sector' | 'watchlist' | 'backtest' | 'journal'
const TAB_KEY = 'tradepilot.left_tab'

interface Props {
  provider: string
  interval: string
  lookback: number
  selected: string | null
  onSelect: (r: ScanResult) => void
  onError: (msg: string | null) => void
}

const TABS: Array<[Tab, string]> = [
  ['sector', 'Sector Rotation'],
  ['watchlist', 'Watchlist'],
  ['backtest', 'Backtest'],
  ['journal', 'Journal'],
]

export function LeftRail({
  provider,
  interval,
  lookback,
  selected,
  onSelect,
  onError,
}: Props) {
  const [tab, setTabState] = useState<Tab>(() => {
    const saved = window.localStorage.getItem(TAB_KEY)
    return saved === 'watchlist' || saved === 'backtest' || saved === 'journal'
      ? (saved as Tab)
      : 'sector'
  })
  const setTab = (t: Tab) => {
    setTabState(t)
    window.localStorage.setItem(TAB_KEY, t)
  }

  return (
    <aside className="flex h-full flex-col border-r border-neutral-800 bg-neutral-950">
      <nav className="flex shrink-0 border-b border-neutral-800">
        {TABS.map(([k, label]) => (
          <button
            key={k}
            type="button"
            onClick={() => setTab(k)}
            className={`flex-1 px-2 py-1.5 text-xs font-medium ${
              tab === k
                ? 'border-b-2 border-[var(--color-accent)] text-neutral-100'
                : 'text-neutral-500 hover:text-neutral-300'
            }`}
          >
            {label}
          </button>
        ))}
      </nav>
      {/* Render every tab and toggle visibility via `hidden` so each
       * panel's state, scroll position, in-flight requests, and
       * auto-rescan timers persist across tab switches. The cost is N
       * mounted components instead of 1; for our four tabs that's
       * negligible vs. the UX win of "ranks are still there when I come
       * back to Sector Rotation a minute later." */}
      <div className="min-h-0 flex-1 overflow-hidden p-2">
        <PanelHost active={tab === 'sector'}>
          <SectorRotationPanel
            provider={provider}
            interval={interval}
            lookback={lookback}
            selected={selected}
            onSelect={onSelect}
            onError={onError}
          />
        </PanelHost>
        <PanelHost active={tab === 'watchlist'}>
          <WatchlistPanel
            provider={provider}
            interval={interval}
            lookback={lookback}
            selected={selected}
            onSelect={onSelect}
            onError={onError}
          />
        </PanelHost>
        <PanelHost active={tab === 'backtest'}>
          <BacktestPanel provider={provider} lookback={lookback} onError={onError} />
        </PanelHost>
        <PanelHost active={tab === 'journal'}>
          <JournalPanel />
        </PanelHost>
      </div>
    </aside>
  )
}

/** Wrapper that keeps a tab's content mounted but visibility-toggles
 * it. `hidden` applies `display: none` so the inactive panel takes no
 * layout space, while React keeps its state and effects alive. */
function PanelHost({
  active,
  children,
}: {
  active: boolean
  children: React.ReactNode
}) {
  return (
    <div hidden={!active} className={active ? 'h-full' : ''}>
      {children}
    </div>
  )
}
