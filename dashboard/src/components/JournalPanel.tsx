import { useCallback, useEffect, useState } from 'react'
import { fetchJournalStats, fetchJournalTrades } from '../api'
import type { JournalStats, JournalTrade } from '../types'
import { fmtCurrency, fmtNumber, fmtPct } from '../format'

interface Props {
  /** Optional baseline hit rate from the most recent backtest, for the
   * "live vs backtester" comparison row. Pass null if not available. */
  backtestHitRate?: number | null
}

const REFRESH_INTERVAL_MS = 15_000

/** Trade journal panel — closed round-trips with R-multiple and a small
 * stat strip on top. Polls every 15s; the SSE stream's `trade_closed`
 * events could trigger an immediate refresh, but polling is simpler and
 * covers the case where the user opens the tab cold.
 */
export function JournalPanel({ backtestHitRate = null }: Props) {
  const [trades, setTrades] = useState<JournalTrade[]>([])
  const [stats, setStats] = useState<JournalStats | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [t, s] = await Promise.all([fetchJournalTrades(100), fetchJournalStats()])
      setTrades(t)
      setStats(s)
      setError(null)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    const id = window.setInterval(() => void load(), REFRESH_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [load])

  return (
    <div className="flex h-full flex-col gap-2">
      <StatStrip
        stats={stats}
        backtestHitRate={backtestHitRate}
        loading={loading}
        error={error}
        onRefresh={load}
      />
      <div className="min-h-0 flex-1 overflow-auto rounded border border-neutral-800">
        {trades.length === 0 ? (
          <p className="p-3 text-xs text-neutral-500">
            No closed trades yet. Each fill that closes (or partially closes) a
            position will appear here.
          </p>
        ) : (
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-neutral-950">
              <tr className="border-b border-neutral-800 text-[10px] uppercase tracking-wide text-neutral-500">
                <Th align="left">Closed</Th>
                <Th align="left">Sym</Th>
                <Th align="right">Qty</Th>
                <Th align="right">Entry</Th>
                <Th align="right">Exit</Th>
                <Th align="right">P&L</Th>
                <Th align="right">R</Th>
                <Th align="right">Hold</Th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <TradeRow key={t.id} trade={t} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function StatStrip({
  stats,
  backtestHitRate,
  loading,
  error,
  onRefresh,
}: {
  stats: JournalStats | null
  backtestHitRate: number | null
  loading: boolean
  error: string | null
  onRefresh: () => void
}) {
  if (error) {
    return (
      <div className="rounded border border-[var(--color-danger)] bg-red-950/30 p-2 text-xs text-[var(--color-danger)]">
        Journal error: {error}
      </div>
    )
  }
  const n = stats?.n_trades ?? 0
  const winRate = stats?.win_rate_pct ?? 0
  const liveVsBacktest =
    backtestHitRate != null && backtestHitRate > 0
      ? winRate - backtestHitRate * 100
      : null

  return (
    <div className="rounded border border-neutral-800 bg-neutral-900/40 p-2">
      <div className="mb-1 flex items-center justify-between">
        <h3 className="text-[10px] uppercase tracking-wide text-neutral-400">
          Performance · {n} trade{n === 1 ? '' : 's'}
        </h3>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="rounded border border-neutral-700 px-1.5 py-0.5 text-[10px] hover:bg-neutral-800 disabled:opacity-50"
        >
          {loading ? '…' : '↻'}
        </button>
      </div>
      <div className="grid grid-cols-2 gap-x-2 gap-y-1 text-xs">
        <Stat
          label="Win rate"
          value={n ? `${fmtNumber(winRate)}%` : '—'}
          sub={
            liveVsBacktest != null
              ? `${liveVsBacktest >= 0 ? '+' : ''}${fmtNumber(liveVsBacktest)}pp vs backtest`
              : undefined
          }
        />
        <Stat
          label="Avg R"
          value={n && stats ? fmtNumber(stats.avg_r) : '—'}
        />
        <Stat
          label="Avg P&L"
          value={n && stats ? fmtPct(stats.avg_pnl_pct, true) : '—'}
          color={
            stats && stats.avg_pnl_pct > 0
              ? 'text-[var(--color-accent-dim)]'
              : stats && stats.avg_pnl_pct < 0
                ? 'text-[var(--color-danger)]'
                : undefined
          }
        />
        <Stat
          label="Total $"
          value={n && stats ? fmtCurrency(stats.total_pnl_abs) : '—'}
          color={
            stats && stats.total_pnl_abs > 0
              ? 'text-[var(--color-accent-dim)]'
              : stats && stats.total_pnl_abs < 0
                ? 'text-[var(--color-danger)]'
                : undefined
          }
        />
      </div>
    </div>
  )
}

function Stat({
  label,
  value,
  sub,
  color,
}: {
  label: string
  value: string
  sub?: string
  color?: string
}) {
  return (
    <div className="flex flex-col leading-tight">
      <span className="text-[9px] uppercase tracking-wide text-neutral-500">{label}</span>
      <span className={`num text-xs ${color ?? 'text-neutral-100'}`}>{value}</span>
      {sub && <span className="text-[9px] text-neutral-500">{sub}</span>}
    </div>
  )
}

function TradeRow({ trade }: { trade: JournalTrade }) {
  const win = trade.win === 1
  const closed = new Date(trade.closed_at)
  const closedStr = isNaN(closed.getTime())
    ? trade.closed_at
    : closed.toLocaleString('en-US', {
        timeZone: 'America/New_York',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
  return (
    <tr className="border-b border-neutral-900 hover:bg-neutral-900/40">
      <Td align="left">
        <span className="text-[10px] text-neutral-400">{closedStr}</span>
      </Td>
      <Td align="left">
        <span className="font-semibold">{trade.symbol}</span>
        <span
          className={`ml-1 text-[9px] uppercase ${
            trade.side === 'long'
              ? 'text-[var(--color-accent-dim)]'
              : 'text-[var(--color-danger)]'
          }`}
        >
          {trade.side}
        </span>
      </Td>
      <Td align="right">{fmtNumber(trade.qty)}</Td>
      <Td align="right">{fmtCurrency(trade.entry_avg)}</Td>
      <Td align="right">{fmtCurrency(trade.exit_avg)}</Td>
      <Td align="right">
        <span
          className={
            win ? 'text-[var(--color-accent-dim)]' : 'text-[var(--color-danger)]'
          }
        >
          {fmtCurrency(trade.pnl_abs)}
        </span>
      </Td>
      <Td align="right">
        {trade.r_multiple == null ? (
          <span className="text-neutral-600">—</span>
        ) : (
          <span
            className={
              trade.r_multiple >= 1
                ? 'text-[var(--color-accent-dim)]'
                : trade.r_multiple < 0
                  ? 'text-[var(--color-danger)]'
                  : 'text-neutral-300'
            }
          >
            {fmtNumber(trade.r_multiple)}R
          </span>
        )}
      </Td>
      <Td align="right">{fmtHold(trade.holding_sec)}</Td>
    </tr>
  )
}

function Th({
  align,
  children,
}: {
  align: 'left' | 'right'
  children: React.ReactNode
}) {
  return (
    <th
      className={`px-1.5 py-1 font-medium ${align === 'right' ? 'text-right' : 'text-left'}`}
    >
      {children}
    </th>
  )
}

function Td({
  align,
  children,
}: {
  align: 'left' | 'right'
  children: React.ReactNode
}) {
  return (
    <td
      className={`px-1.5 py-0.5 ${align === 'right' ? 'text-right num' : 'text-left'}`}
    >
      {children}
    </td>
  )
}

function fmtHold(sec: number): string {
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.round(sec / 60)}m`
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h`
  return `${(sec / 86400).toFixed(1)}d`
}
