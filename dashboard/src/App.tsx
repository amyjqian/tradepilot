import { useState } from 'react'
import { Panel, PanelGroup } from 'react-resizable-panels'
import type { BrokerPosition, ScanResult } from './types'
import { TopBar } from './components/TopBar'
import { LeftRail } from './components/LeftRail'
import { CenterPane } from './components/CenterPane'
import { RightRail } from './components/RightRail'
import { LiveConfirmModal } from './components/LiveConfirmModal'
import { ResizeHandle } from './components/ResizeHandle'
import { ConnectView } from './components/ConnectView'
import { WarningToasts } from './components/WarningToasts'
import { useBrokerData } from './useBrokerData'

type AppView = 'trade' | 'connect'
const VIEW_KEY = 'tradepilot.app_view'

type Provider = 'synthetic' | 'yfinance' | 'ibkr' | 'polygon'
type Interval = '1d' | '1h' | '15m' | '5m' | '1m'

const DEFAULT_LOOKBACK: Record<Interval, number> = {
  '1d': 90,
  '1h': 30,
  '15m': 10,
  '5m': 5,
  '1m': 2,
}

export default function App() {
  const [view, setViewState] = useState<AppView>(() => {
    const saved = window.localStorage.getItem(VIEW_KEY)
    return saved === 'connect' ? 'connect' : 'trade'
  })
  const setView = (v: AppView) => {
    setViewState(v)
    window.localStorage.setItem(VIEW_KEY, v)
  }
  const [provider, setProvider] = useState<Provider>('polygon')
  const [interval, setInterval] = useState<Interval>('1d')
  const [lookback, setLookback] = useState<number>(DEFAULT_LOOKBACK['1d'])
  const [selected, setSelected] = useState<ScanResult | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const {
    status,
    account,
    positions,
    orders,
    risk,
    accounts,
    selectedAccount,
    setSelectedAccount,
    connections,
    selectedTargets,
    setSelectedTargets,
    refresh,
    resetKillSwitch,
    liveAcknowledged,
    acknowledgeLive,
  } = useBrokerData()

  /**
   * Pending callback to run once the user types "LIVE" in the session
   * acknowledgement modal. Only set when broker is live and the user has
   * not yet acknowledged. Cleared on approve or cancel.
   */
  const [livePendingApprove, setLivePendingApprove] = useState<(() => void) | null>(null)

  const requestLiveConfirm = (onApprove: () => void) => {
    setLivePendingApprove(() => onApprove)
  }

  const onLiveApproved = () => {
    acknowledgeLive()
    const cb = livePendingApprove
    setLivePendingApprove(null)
    if (cb) cb()
  }

  const onLiveCanceled = () => {
    setLivePendingApprove(null)
  }

  const changeInterval = (next: Interval) => {
    setInterval(next)
    setLookback(DEFAULT_LOOKBACK[next])
  }

  const pickPosition = (p: BrokerPosition) => {
    setSelected({
      ticker: p.symbol,
      score: 0,
      price: p.current_price || p.avg_entry_price,
      pct_change: p.unrealized_pl_pct,
      rel_volume: 0,
      rsi: 0,
      above_vwap: false,
      above_ema9: false,
      ema_stacked: false,
      dist_from_20d_high_pct: 0,
      signals: {},
      reasons: [`Held position: ${p.qty} @ ${p.avg_entry_price}`],
    })
  }

  const onAfterOrder = () => {
    void refresh()
  }

  return (
    <div className="flex h-screen flex-col bg-neutral-950 text-neutral-100">
      <TopBar
        provider={provider}
        setProvider={setProvider}
        interval={interval}
        setInterval={changeInterval}
        lookback={lookback}
        setLookback={setLookback}
        err={err}
        brokerStatus={status}
        account={account}
        positions={positions}
        risk={risk}
        onAfterKillSwitch={onAfterOrder}
        onResetRisk={resetKillSwitch}
        onError={setErr}
      />

      <ViewTabs view={view} onChange={setView} />

      <main className="min-h-0 flex-1">
        {view === 'connect' ? (
          <ConnectView onError={setErr} />
        ) : (
          <PanelGroup
            direction="horizontal"
            autoSaveId="tradepilot-mainsplit"
            className="h-full"
          >
            <Panel defaultSize={20} minSize={12} maxSize={40} className="min-w-0">
              <LeftRail
                provider={provider}
                interval={interval}
                lookback={lookback}
                selected={selected?.ticker ?? null}
                onSelect={setSelected}
                onError={setErr}
              />
            </Panel>
            <ResizeHandle />
            <Panel defaultSize={60} minSize={30} className="min-w-0">
              <CenterPane selected={selected} />
            </Panel>
            <ResizeHandle />
            <Panel defaultSize={20} minSize={12} maxSize={40} className="min-w-0">
              <RightRail
                selected={selected}
                brokerStatus={status}
                positions={positions}
                orders={orders}
                accounts={accounts}
                selectedAccount={selectedAccount}
                onSelectAccount={setSelectedAccount}
                connections={connections}
                selectedTargets={selectedTargets}
                onSelectTargets={setSelectedTargets}
                liveAcknowledged={liveAcknowledged}
                onLiveConfirmRequested={requestLiveConfirm}
                onPickPosition={pickPosition}
                onAfterOrder={onAfterOrder}
                onError={setErr}
              />
            </Panel>
          </PanelGroup>
        )}
      </main>

      {livePendingApprove && (
        <LiveConfirmModal onApprove={onLiveApproved} onCancel={onLiveCanceled} />
      )}

      <WarningToasts />
    </div>
  )
}

function ViewTabs({
  view,
  onChange,
}: {
  view: AppView
  onChange: (v: AppView) => void
}) {
  const tabs: { id: AppView; label: string }[] = [
    { id: 'trade', label: 'Trade' },
    { id: 'connect', label: 'Connect' },
  ]
  return (
    <nav className="flex shrink-0 border-b border-neutral-800 bg-neutral-950">
      {tabs.map((t) => {
        const active = view === t.id
        return (
          <button
            key={t.id}
            type="button"
            onClick={() => onChange(t.id)}
            className={`px-3 py-1 text-[11px] font-semibold uppercase tracking-wide transition-colors ${
              active
                ? 'border-b-2 border-[var(--color-accent)] text-neutral-100'
                : 'border-b-2 border-transparent text-neutral-500 hover:text-neutral-200'
            }`}
          >
            {t.label}
          </button>
        )
      })}
    </nav>
  )
}
