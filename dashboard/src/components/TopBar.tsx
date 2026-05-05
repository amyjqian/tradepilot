import { useEffect, useState } from 'react'
import type {
  AccountSnapshot,
  BrokerPosition,
  BrokerStatus,
  RiskStatus,
} from '../types'
import { fmtCurrency, fmtPct } from '../format'
import { KillSwitchModal } from './KillSwitchModal'

type Provider = 'synthetic' | 'yfinance' | 'ibkr' | 'polygon'
type Interval = '1d' | '1h' | '30m' | '15m' | '5m' | '2m' | '1m'

interface Props {
  provider: Provider
  setProvider: (p: Provider) => void
  interval: Interval
  setInterval: (i: Interval) => void
  lookback: number
  setLookback: (n: number) => void
  err: string | null
  brokerStatus: BrokerStatus | null
  account: AccountSnapshot | null
  positions: BrokerPosition[]
  risk: RiskStatus | null
  onAfterKillSwitch: () => void
  onResetRisk: () => Promise<void>
  onError: (msg: string) => void
}

function useEasternClock(): string {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])
  return now.toLocaleTimeString('en-US', {
    timeZone: 'America/New_York',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function isMarketOpen(): boolean {
  const now = new Date()
  const et = new Date(
    now.toLocaleString('en-US', { timeZone: 'America/New_York' }),
  )
  const day = et.getDay()
  if (day === 0 || day === 6) return false
  const minutes = et.getHours() * 60 + et.getMinutes()
  return minutes >= 9 * 60 + 30 && minutes < 16 * 60
}

export function TopBar({
  provider,
  setProvider,
  interval,
  setInterval,
  lookback,
  setLookback,
  err,
  brokerStatus,
  account,
  positions,
  risk,
  onAfterKillSwitch,
  onResetRisk,
  onError,
}: Props) {
  const clock = useEasternClock()
  const open = isMarketOpen()
  const [killOpen, setKillOpen] = useState(false)
  const connected = brokerStatus?.connected ?? false
  const paper = brokerStatus?.paper ?? true

  return (
    <header className="border-b border-neutral-800 bg-neutral-950 px-4 py-2 text-neutral-100">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h1 className="text-sm font-semibold tracking-tight">TradePilot</h1>
          <span className="rounded border border-neutral-700 px-1.5 py-0.5 text-[10px] uppercase text-neutral-400">
            phase 3
          </span>
          {connected && (
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
                paper
                  ? 'bg-amber-900/40 text-amber-300'
                  : 'bg-red-900/40 text-red-300'
              }`}
              title={
                brokerStatus?.account
                  ? `${paper ? 'Paper' : 'LIVE'} · ${brokerStatus.account}`
                  : paper
                    ? 'Paper trading'
                    : 'LIVE trading'
              }
            >
              {paper ? 'Paper' : 'Live'}
              {brokerStatus?.account ? (
                <span className="ml-1 font-mono">{brokerStatus.account}</span>
              ) : null}
            </span>
          )}
        </div>

        <AccountStrip account={account} connected={connected} />
        {connected && risk?.enabled && (
          <DrawdownPill risk={risk} onReset={onResetRisk} onError={onError} />
        )}

        <div className="flex items-center gap-2 text-xs">
          <span className="num text-neutral-300">{clock} ET</span>
          <span
            className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
              open
                ? 'bg-[var(--color-accent)]/20 text-[var(--color-accent)]'
                : 'bg-neutral-800 text-neutral-400'
            }`}
          >
            {open ? 'Market Open' : 'Closed'}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value as Provider)}
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1"
            title="Data provider"
          >
            <option value="synthetic">Synthetic</option>
            <option value="yfinance">yfinance</option>
            <option value="ibkr">IBKR</option>
            <option value="polygon">Polygon</option>
          </select>
          <select
            value={interval}
            onChange={(e) => setInterval(e.target.value as Interval)}
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1"
            title="Bar interval"
          >
            <option value="1d">1d</option>
            <option value="1h">1h</option>
            <option value="30m">30m</option>
            <option value="15m">15m</option>
            <option value="5m">5m</option>
            <option value="2m">2m</option>
            <option value="1m">1m</option>
          </select>
          <input
            type="number"
            value={lookback}
            onChange={(e) => setLookback(Math.max(1, Number(e.target.value) || 1))}
            className="w-14 rounded border border-neutral-700 bg-neutral-900 px-2 py-1"
            title="Lookback days"
            min={1}
          />
          <button
            type="button"
            onClick={() => setKillOpen(true)}
            disabled={!connected}
            className={`rounded border px-2 py-1 font-semibold ${
              connected
                ? 'border-[var(--color-danger)] text-[var(--color-danger)] hover:bg-[var(--color-danger)]/15'
                : 'cursor-not-allowed border-neutral-700 text-neutral-600 opacity-60'
            }`}
            title={
              connected
                ? 'Close all positions immediately'
                : 'Connect IBKR (set IB_BROKER_PORT and start TWS) to enable'
            }
          >
            Kill Switch
          </button>
        </div>
      </div>

      {err && (
        <div className="mt-2 rounded border border-[var(--color-danger)] bg-red-950/40 px-2 py-1 text-xs text-[var(--color-danger)]">
          {err}
        </div>
      )}

      {killOpen && (
        <KillSwitchModal
          positions={positions}
          paper={paper}
          onClose={() => setKillOpen(false)}
          onAfterClose={onAfterKillSwitch}
          onError={onError}
        />
      )}
    </header>
  )
}

function AccountStrip({
  account,
  connected,
}: {
  account: AccountSnapshot | null
  connected: boolean
}) {
  if (!connected) {
    return (
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-wide text-neutral-500">
        <span className="rounded border border-neutral-700 px-1.5 py-0.5">
          Broker not connected
        </span>
      </div>
    )
  }
  if (!account) {
    return (
      <div className="text-[10px] uppercase tracking-wide text-neutral-500">Loading account…</div>
    )
  }
  const pnlPositive = account.pnl_today_abs >= 0
  return (
    <div className="flex items-center gap-3 text-xs">
      <Stat label="Equity" value={fmtCurrency(account.equity)} />
      <Stat
        label="P&L Today"
        value={`${fmtCurrency(account.pnl_today_abs)} · ${fmtPct(account.pnl_today_pct, true)}`}
        color={
          pnlPositive ? 'text-[var(--color-accent-dim)]' : 'text-[var(--color-danger)]'
        }
      />
      <Stat label="Buying Power" value={fmtCurrency(account.buying_power)} />
      <Stat label="Cash" value={fmtCurrency(account.cash)} />
    </div>
  )
}

function Stat({
  label,
  value,
  color,
}: {
  label: string
  value: string
  color?: string
}) {
  return (
    <div className="flex flex-col leading-tight">
      <span className="text-[9px] uppercase tracking-wide text-neutral-500">{label}</span>
      <span className={`num text-xs ${color ?? 'text-neutral-100'}`}>{value}</span>
    </div>
  )
}

function DrawdownPill({
  risk,
  onReset,
  onError,
}: {
  risk: RiskStatus
  onReset: () => Promise<void>
  onError: (msg: string) => void
}) {
  const dd = risk.drawdown_pct ?? 0
  const limit = risk.limit_pct
  // Drawdown is negative when at a loss; the bar fills as we approach -limit.
  const fillPct = Math.min(100, Math.max(0, (-dd / limit) * 100))
  const tripped = risk.kill_active
  const color = tripped
    ? 'var(--color-danger)'
    : fillPct > 70
      ? '#f59e0b' /* amber-500 */
      : 'var(--color-accent-dim)'

  const handleReset = async () => {
    if (!window.confirm(
      'Reset the daily-loss circuit breaker? New orders will be allowed until the limit is breached again.',
    )) return
    try {
      await onReset()
    } catch (e) {
      onError(String(e))
    }
  }

  return (
    <div
      className={`flex items-center gap-2 rounded border px-2 py-1 text-xs ${
        tripped
          ? 'border-[var(--color-danger)] bg-red-950/40'
          : 'border-neutral-700 bg-neutral-900/40'
      }`}
      title={
        tripped
          ? `LOCKED — ${risk.kill_reason ?? 'daily drawdown limit hit'}`
          : `Today's drawdown vs daily-loss limit (${fmtPct(limit, false)})`
      }
    >
      <div className="flex flex-col leading-tight">
        <span className="text-[9px] uppercase tracking-wide text-neutral-500">
          {tripped ? 'Locked' : 'Daily DD'}
        </span>
        <span
          className="num text-xs"
          style={{ color: tripped ? 'var(--color-danger)' : undefined }}
        >
          {risk.drawdown_pct === null ? '—' : fmtPct(dd, true)}
          <span className="text-neutral-500"> / {fmtPct(-limit, true)}</span>
        </span>
      </div>
      <div className="h-3 w-14 overflow-hidden rounded bg-neutral-800">
        <div
          className="h-full transition-[width]"
          style={{ width: `${fillPct}%`, backgroundColor: color }}
        />
      </div>
      {tripped && (
        <button
          type="button"
          onClick={handleReset}
          className="rounded border border-[var(--color-danger)] px-1.5 py-0.5 text-[10px] font-semibold text-[var(--color-danger)] hover:bg-[var(--color-danger)]/15"
          title="Manually re-arm — new orders allowed after reset"
        >
          Reset
        </button>
      )}
    </div>
  )
}
