import { useState } from 'react'
import type { ScanResult } from '../types'
import { SectorRotationPanel } from './SectorRotationPanel'
import { WatchlistPanel } from './WatchlistPanel'
import { BacktestPanel } from './BacktestPanel'
import { JournalPanel } from './JournalPanel'

type Tab = 'sector' | 'watchlist' | 'backtest' | 'journal'

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
  const [tab, setTab] = useState<Tab>('sector')

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
      <div className="min-h-0 flex-1 overflow-hidden p-2">
        {tab === 'sector' && (
          <SectorRotationPanel
            provider={provider}
            interval={interval}
            lookback={lookback}
            selected={selected}
            onSelect={onSelect}
            onError={onError}
          />
        )}
        {tab === 'watchlist' && (
          <WatchlistPanel
            provider={provider}
            interval={interval}
            lookback={lookback}
            selected={selected}
            onSelect={onSelect}
            onError={onError}
          />
        )}
        {tab === 'backtest' && (
          <BacktestPanel provider={provider} lookback={lookback} onError={onError} />
        )}
        {tab === 'journal' && <JournalPanel />}
      </div>
    </aside>
  )
}
