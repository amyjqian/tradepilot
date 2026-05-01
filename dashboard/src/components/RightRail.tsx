import { useState } from 'react'
import { Panel, PanelGroup } from 'react-resizable-panels'
import { closePosition } from '../api'
import type {
  AccountSnapshot,
  BrokerPosition,
  BrokerStatus,
  OrderRecord,
  ScanResult,
} from '../types'
import { fmtCurrency, fmtPct } from '../format'
import { OrderTicket } from './OrderTicket'
import { ResizeHandle } from './ResizeHandle'

interface Props {
  selected: ScanResult | null
  brokerStatus: BrokerStatus | null
  account: AccountSnapshot | null
  positions: BrokerPosition[]
  orders: OrderRecord[]
  accounts: string[]
  selectedAccount: string | null
  onSelectAccount: (acct: string | null) => void
  liveAcknowledged: boolean
  onLiveConfirmRequested: (onApprove: () => void) => void
  onPickPosition: (p: BrokerPosition) => void
  onAfterOrder: () => void
  onError: (msg: string) => void
}

export function RightRail({
  selected,
  brokerStatus,
  account,
  positions,
  orders,
  accounts,
  selectedAccount,
  onSelectAccount,
  liveAcknowledged,
  onLiveConfirmRequested,
  onPickPosition,
  onAfterOrder,
  onError,
}: Props) {
  const connected = brokerStatus?.connected ?? false
  const paper = brokerStatus?.paper ?? true

  return (
    <aside className="flex h-full flex-col border-l border-neutral-800 bg-neutral-950">
      <PanelGroup
        direction="vertical"
        autoSaveId="bullish-rightrail"
        className="h-full"
      >
        <Panel defaultSize={32} minSize={15} className="min-h-0">
          <Section title="Order Ticket">
            {connected ? (
              <OrderTicket
                selected={selected}
                account={account}
                paper={paper}
                accounts={accounts}
                selectedAccount={selectedAccount}
                onSelectAccount={onSelectAccount}
                liveAcknowledged={liveAcknowledged}
                onLiveConfirmRequested={onLiveConfirmRequested}
                onAfterOrder={onAfterOrder}
                onError={onError}
              />
            ) : (
              <p className="text-xs text-neutral-500">
                Connect IBKR (TWS / IB Gateway) to place orders.
              </p>
            )}
          </Section>
        </Panel>
        <ResizeHandle direction="vertical" />
        <Panel defaultSize={34} minSize={10} className="min-h-0">
          <Section
            title={`Open Positions${connected ? ` (${positions.length})` : ''}`}
            badge={connected ? undefined : 'not connected'}
          >
            {!connected ? (
              <div className="space-y-1 text-xs text-neutral-500">
                <p>{brokerStatus?.hint ?? 'Connect IBKR to see live positions.'}</p>
                <p className="text-[10px]">
                  Make sure TWS or IB Gateway is running and accepting API connections,
                  then set <code className="rounded bg-neutral-800 px-1">IB_BROKER_PORT</code>{' '}
                  in <code>~/.config/bullish_scanner/ib.env</code> and restart the API.
                </p>
              </div>
            ) : positions.length === 0 ? (
              <p className="text-xs text-neutral-500">No open positions.</p>
            ) : (
              <ul className="divide-y divide-neutral-900">
                {positions.map((p) => (
                  <PositionRow
                    key={p.symbol}
                    position={p}
                    paper={paper}
                    selectedAccount={selectedAccount}
                    liveAcknowledged={liveAcknowledged}
                    onClick={() => onPickPosition(p)}
                    onLiveConfirmRequested={onLiveConfirmRequested}
                    onAfterClose={onAfterOrder}
                    onError={onError}
                  />
                ))}
              </ul>
            )}
          </Section>
        </Panel>
        <ResizeHandle direction="vertical" />
        <Panel defaultSize={34} minSize={10} className="min-h-0">
          <Section title="Recent Activity">
            {!connected ? (
              <p className="text-xs text-neutral-500">Connect IBKR to see recent orders.</p>
            ) : orders.length === 0 ? (
              <p className="text-xs text-neutral-500">No recent orders.</p>
            ) : (
              <ul className="divide-y divide-neutral-900">
                {orders.slice(0, 12).map((o) => (
                  <OrderRow key={o.id} order={o} />
                ))}
              </ul>
            )}
          </Section>
        </Panel>
      </PanelGroup>
    </aside>
  )
}

function PositionRow({
  position,
  paper,
  selectedAccount,
  liveAcknowledged,
  onClick,
  onLiveConfirmRequested,
  onAfterClose,
  onError,
}: {
  position: BrokerPosition
  paper: boolean
  selectedAccount: string | null
  liveAcknowledged: boolean
  onClick: () => void
  onLiveConfirmRequested: (onApprove: () => void) => void
  onAfterClose: () => void
  onError: (msg: string) => void
}) {
  const p = position
  const positive = p.unrealized_pl_abs >= 0
  const [pending, setPending] = useState<number | null>(null)

  const closeFraction = async (pct: number) => {
    if (!paper && !liveAcknowledged) {
      onLiveConfirmRequested(() => void doClose(pct))
      return
    }
    void doClose(pct)
  }

  const doClose = async (pct: number) => {
    if (!window.confirm(`Close ${pct}% of ${p.symbol} (${p.qty} shares)?`)) return
    setPending(pct)
    try {
      await closePosition(p.symbol, {
        percentage: pct,
        account: selectedAccount ?? undefined,
      })
      onAfterClose()
    } catch (e) {
      onError(String(e))
    } finally {
      setPending(null)
    }
  }

  return (
    <li className="space-y-1 px-1 py-1">
      <button
        type="button"
        onClick={onClick}
        className="flex w-full items-center justify-between gap-2 text-left text-xs hover:bg-neutral-900"
      >
        <span className="flex flex-col leading-tight">
          <span className="font-semibold">{p.symbol}</span>
          <span className="text-[10px] text-neutral-500">
            {p.qty} @ {fmtCurrency(p.avg_entry_price)}
          </span>
        </span>
        <span className="flex flex-col items-end leading-tight">
          <span
            className={`num ${
              positive
                ? 'text-[var(--color-accent-dim)]'
                : 'text-[var(--color-danger)]'
            }`}
          >
            {fmtCurrency(p.unrealized_pl_abs)}
          </span>
          <span
            className={`num text-[10px] ${
              positive
                ? 'text-[var(--color-accent-dim)]'
                : 'text-[var(--color-danger)]'
            }`}
          >
            {fmtPct(p.unrealized_pl_pct, true)}
          </span>
        </span>
      </button>
      <div className="flex gap-1">
        {[25, 50, 100].map((pct) => (
          <button
            key={pct}
            type="button"
            onClick={() => closeFraction(pct)}
            disabled={pending !== null}
            className={`flex-1 rounded border border-neutral-700 px-1 py-0.5 text-[10px] font-semibold hover:bg-neutral-800 disabled:opacity-50 ${
              pct === 100 ? 'text-[var(--color-danger)]' : 'text-neutral-300'
            }`}
            title={`Close ${pct}% of ${p.symbol}`}
          >
            {pending === pct ? '…' : `${pct}%`}
          </button>
        ))}
      </div>
    </li>
  )
}

function OrderRow({ order }: { order: OrderRecord }) {
  const o = order
  const filledPx = o.filled_avg_price
  const time = o.submitted_at ? new Date(o.submitted_at) : null
  const timeStr = time
    ? time.toLocaleTimeString('en-US', {
        timeZone: 'America/New_York',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '—'

  const statusColor: string =
    o.status === 'filled'
      ? 'text-[var(--color-accent-dim)]'
      : o.status === 'rejected' || o.status === 'canceled'
        ? 'text-[var(--color-danger)]'
        : 'text-neutral-400'

  return (
    <li className="flex items-center justify-between gap-2 px-1 py-0.5 text-xs">
      <span className="flex items-center gap-1.5">
        <span
          className={`text-[10px] font-bold ${
            o.side === 'buy'
              ? 'text-[var(--color-accent)]'
              : 'text-[var(--color-danger)]'
          }`}
        >
          {o.side.toUpperCase()}
        </span>
        <span className="font-semibold">{o.symbol}</span>
        <span className="num text-[10px] text-neutral-500">{o.qty}</span>
      </span>
      <span className="flex items-center gap-1.5">
        {filledPx !== null && (
          <span className="num text-[10px] text-neutral-400">{fmtCurrency(filledPx)}</span>
        )}
        <span className={`text-[10px] uppercase ${statusColor}`}>{o.status}</span>
        <span className="num text-[10px] text-neutral-600">{timeStr}</span>
      </span>
    </li>
  )
}

function Section({
  title,
  badge,
  children,
}: {
  title: string
  badge?: string
  children: React.ReactNode
}) {
  return (
    <section className="m-1 flex h-[calc(100%-0.5rem)] flex-col rounded border border-neutral-800 bg-neutral-900/40 p-2">
      <div className="mb-1.5 flex shrink-0 items-center justify-between">
        <h3 className="text-[10px] uppercase tracking-wide text-neutral-400">{title}</h3>
        {badge && (
          <span className="rounded border border-neutral-700 px-1 py-0 text-[9px] uppercase tracking-wide text-neutral-500">
            {badge}
          </span>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </section>
  )
}
