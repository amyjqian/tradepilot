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
import { ScoringView } from './components/ScoringView'
import { WarningToasts } from './components/WarningToasts'
import { useBrokerData } from './useBrokerData'

type AppView = 'trade' | 'scoring' | 'connect'
const VIEW_KEY = 'tradepilot.app_view'

type Provider = 'synthetic' | 'yfinance' | 'ibkr' | 'polygon'
type Interval = '1d' | '1h' | '30m' | '15m' | '5m' | '2m' | '1m'

// Persisted across reloads so the user doesn't land back on 1d every time
// they refresh after picking 5m or 1h. Same lifecycle / pattern as VIEW_KEY.
const PROVIDER_KEY = 'tradepilot.provider'
const INTERVAL_KEY = 'tradepilot.interval'
const LOOKBACK_KEY = 'tradepilot.lookback'

const VALID_PROVIDERS: ReadonlySet<Provider> = new Set([
  'synthetic',
  'yfinance',
  'ibkr',
  'polygon',
])
const VALID_INTERVALS: ReadonlySet<Interval> = new Set([
  '1d',
  '1h',
  '30m',
  '15m',
  '5m',
  '2m',
  '1m',
])

// Tuned for paid Polygon. The previous 1m=2 / 5m=5 defaults were inherited
// from the IB era — IB caps 1-min requests at 1 day per request and 5-min at
// 30, and the 10-min pacing budget made larger pulls expensive. Polygon's
// paid tier returns weeks of intraday in a single call, and our parquet cache
// only fetches deltas after the first hit. Bigger windows mean better EMA50
// warmup (200 bars ≈ 3.3h on 1m) and the prior-session close is always inside
// the lookback, so the Monday-after-weekend re-anchor crutch isn't needed.
const DEFAULT_LOOKBACK: Record<Interval, number> = {
  '1d': 90,
  '1h': 60,
  '30m': 30,
  '15m': 30,
  '5m': 15,
  '2m': 7,
  '1m': 5,
}

export default function App() {
  const [view, setViewState] = useState<AppView>(() => {
    const saved = window.localStorage.getItem(VIEW_KEY)
    if (saved === 'connect' || saved === 'scoring') return saved
    return 'trade'
  })
  const setView = (v: AppView) => {
    setViewState(v)
    window.localStorage.setItem(VIEW_KEY, v)
  }
  const [provider, setProviderState] = useState<Provider>(() => {
    const saved = window.localStorage.getItem(PROVIDER_KEY)
    return saved && VALID_PROVIDERS.has(saved as Provider)
      ? (saved as Provider)
      : 'polygon'
  })
  const [interval, setIntervalState] = useState<Interval>(() => {
    const saved = window.localStorage.getItem(INTERVAL_KEY)
    return saved && VALID_INTERVALS.has(saved as Interval)
      ? (saved as Interval)
      : '5m'
  })
  // Lookback is interval-derived by default but the user can override via the
  // input. Persist the explicit value so the override survives reloads, but
  // gate on numeric sanity so a corrupted localStorage entry doesn't push a
  // negative or non-numeric lookback to the API.
  const [lookback, setLookbackState] = useState<number>(() => {
    const saved = Number(window.localStorage.getItem(LOOKBACK_KEY))
    if (Number.isFinite(saved) && saved >= 1) return saved
    const startInterval = (window.localStorage.getItem(INTERVAL_KEY) ?? '5m') as Interval
    return DEFAULT_LOOKBACK[VALID_INTERVALS.has(startInterval) ? startInterval : '5m']
  })
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

  const setProvider = (p: Provider) => {
    setProviderState(p)
    window.localStorage.setItem(PROVIDER_KEY, p)
  }

  const setLookback = (n: number) => {
    setLookbackState(n)
    window.localStorage.setItem(LOOKBACK_KEY, String(n))
  }

  const changeInterval = (next: Interval) => {
    setIntervalState(next)
    window.localStorage.setItem(INTERVAL_KEY, next)
    // Reset lookback to the new interval's sensible default — keeping the
    // previous lookback (e.g. 90 from 1d) when switching to 1m would force
    // a huge fetch. The user can override afterwards via the input.
    const nextLookback = DEFAULT_LOOKBACK[next]
    setLookbackState(nextLookback)
    window.localStorage.setItem(LOOKBACK_KEY, String(nextLookback))
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

      <ViewTabs
        view={view}
        onChange={setView}
        brokerDisabled={status?.disabled ?? false}
      />

      <main className="min-h-0 flex-1">
        {view === 'connect' && status?.disabled ? (
          <div className="flex h-full items-center justify-center text-xs text-neutral-500">
            Broker is disabled (TRADEPILOT_BROKER_DISABLED). Switch to Trade or
            Scoring.
          </div>
        ) : view === 'connect' ? (
          <ConnectView onError={setErr} />
        ) : view === 'scoring' ? (
          <ScoringView provider={provider} />
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
  brokerDisabled,
}: {
  view: AppView
  onChange: (v: AppView) => void
  brokerDisabled: boolean
}) {
  const tabs: { id: AppView; label: string }[] = [
    { id: 'trade', label: 'Trade' },
    { id: 'scoring', label: 'Scoring' },
    // Connect tab is broker-only — hide when IBKR is disabled.
    ...(brokerDisabled ? [] : [{ id: 'connect' as AppView, label: 'Connect' }]),
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
