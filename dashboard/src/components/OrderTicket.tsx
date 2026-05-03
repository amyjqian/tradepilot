import { useEffect, useMemo, useState } from 'react'
import { submitOrder } from '../api'
import type { AccountSnapshot, ScanResult } from '../types'
import { fmtCurrency, fmtNumber } from '../format'

const RISK_PCT_KEY = 'tradepilot.risk_pct'
const DEFAULT_RISK_PCT = 0.5

// Tick size for US stocks priced ≥ $1. Sub-dollar stocks technically use
// 0.0001, but the order ticket targets normal liquid names where penny
// ticks are correct. If we ever need true sub-dollar pricing, gate this
// on `last < 1`.
const TICK_SIZE = 0.01

// Hard cap distance from `last` for the Pegged-to-Primary `cap_price`
// (lmtPrice ceiling for BUY, floor for SELL). $0.50 is generous enough
// that normal intraday wobble won't bump into it, tight enough that a
// runaway tape can't blow your account up.
const PEG_CAP_OFFSET = 0.5

const HOT_QTYS = [10, 25, 50, 100, 200, 500] as const

type Side = 'buy' | 'sell'
type HotKind = 'mkt' | 'bid_plus_tick'

interface Props {
  selected: ScanResult | null
  account: AccountSnapshot | null
  paper: boolean
  /** All managed IB accounts visible from the connection. */
  accounts: string[]
  /** Currently-selected routing account, or null to use broker default. */
  selectedAccount: string | null
  onSelectAccount: (acct: string | null) => void
  liveAcknowledged: boolean
  onLiveConfirmRequested: (onApprove: () => void) => void
  onAfterOrder: () => void
  onError: (msg: string) => void
}

/** Persistent risk % stored in localStorage. */
function useRiskPct(): [number, (n: number) => void] {
  const [v, setV] = useState<number>(() => {
    const saved = window.localStorage.getItem(RISK_PCT_KEY)
    const parsed = saved ? Number(saved) : NaN
    return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_RISK_PCT
  })
  useEffect(() => {
    window.localStorage.setItem(RISK_PCT_KEY, String(v))
  }, [v])
  return [v, setV]
}

export function OrderTicket({
  selected,
  account,
  paper,
  accounts,
  selectedAccount,
  onSelectAccount,
  liveAcknowledged,
  onLiveConfirmRequested,
  onAfterOrder,
  onError,
}: Props) {
  const [side, setSide] = useState<Side>('buy')
  const [stopPrice, setStopPrice] = useState<string>('')
  const [overrideQty, setOverrideQty] = useState<string>('')
  const [riskPct, setRiskPct] = useRiskPct()
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  const last = selected?.price ?? 0
  const equity = account?.equity ?? 0
  const riskDollars = (equity * riskPct) / 100

  // Reset inputs when ticker changes so we don't leak a stop value across symbols.
  useEffect(() => {
    setStopPrice('')
    setOverrideQty('')
  }, [selected?.ticker])

  const computed = useMemo(() => {
    const stop = Number(stopPrice)
    const overrideN = Number(overrideQty)
    if (overrideQty && overrideN > 0) {
      return { qty: overrideN, riskPerShare: 0, source: 'manual' as const }
    }
    if (!stop || !last || side === 'sell') return null
    const riskPerShare = last - stop
    if (riskPerShare <= 0) return null
    const qtyRaw = riskDollars / riskPerShare
    const qty = Math.max(1, Math.floor(qtyRaw))
    return { qty, riskPerShare, source: 'risk' as const }
  }, [stopPrice, overrideQty, last, side, riskDollars])

  const targets = useMemo(() => {
    if (!computed || computed.source !== 'risk' || !last) return null
    const r = computed.riskPerShare
    return [1, 2, 3].map((m) => ({ r: m, price: last + m * r }))
  }, [computed, last])

  if (!selected) {
    return (
      <p className="text-xs text-neutral-500">Select a ticker to load the ticket.</p>
    )
  }

  const qty = computed?.qty ?? 0
  const symbol = selected.ticker
  const ready = qty > 0 && symbol.length > 0

  const sendOrder = async () => {
    setSubmitting(true)
    try {
      // Capture the stop and the score at the moment of submission so
      // the journal can compute R-multiple later. Both are optional —
      // a sell-side close has no stop, and a manually-entered ticker
      // outside a scan has no score.
      const stopValue = Number(stopPrice)
      const planned_stop =
        side === 'buy' && Number.isFinite(stopValue) && stopValue > 0
          ? stopValue
          : undefined
      const score_at_entry =
        selected && Number.isFinite(selected.score) && selected.score !== 0
          ? selected.score
          : undefined
      await submitOrder({
        symbol,
        qty,
        side,
        type: 'market',
        time_in_force: 'day',
        ...(planned_stop !== undefined ? { planned_stop } : {}),
        ...(score_at_entry !== undefined ? { score_at_entry } : {}),
        ...(selectedAccount ? { account: selectedAccount } : {}),
      })
      setConfirmOpen(false)
      onAfterOrder()
    } catch (e) {
      onError(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  /** One-click hot-button submit. Skips the confirm modal on paper; on
   * live it follows the same `liveAcknowledged` gate as the main
   * submit button (one confirm per session, then fast clicks). */
  const fireHotOrder = async (
    hotSide: Side,
    kind: HotKind,
    hotQty: number,
  ) => {
    if (!last) {
      onError('Cannot place hot order: last price unknown')
      return
    }
    if (!paper && !liveAcknowledged) {
      onLiveConfirmRequested(() => {
        void doHotOrder(hotSide, kind, hotQty)
      })
      return
    }
    void doHotOrder(hotSide, kind, hotQty)
  }

  const doHotOrder = async (hotSide: Side, kind: HotKind, hotQty: number) => {
    setSubmitting(true)
    try {
      const score_at_entry =
        selected && Number.isFinite(selected.score) && selected.score !== 0
          ? selected.score
          : undefined
      // `@Bid+1t` / `@Ask-1t` ride IB's Pegged-to-Primary (REL) order:
      // the working price tracks the primary exchange best bid (BUY) or
      // ask (SELL) plus/minus `peg_offset`, with `cap_price` as a hard
      // ceiling/floor so a runaway tape can't blow past $0.50 from last.
      const isLimitPeg = kind === 'bid_plus_tick'
      const cap_price = isLimitPeg
        ? hotSide === 'buy'
          ? round2(last + PEG_CAP_OFFSET)
          : Math.max(0.01, round2(last - PEG_CAP_OFFSET))
        : undefined
      await submitOrder({
        symbol,
        qty: hotQty,
        side: hotSide,
        type: isLimitPeg ? 'pegprim' : 'market',
        time_in_force: 'day',
        ...(isLimitPeg ? { peg_offset: TICK_SIZE, cap_price } : {}),
        ...(score_at_entry !== undefined ? { score_at_entry } : {}),
        ...(selectedAccount ? { account: selectedAccount } : {}),
      })
      onAfterOrder()
    } catch (e) {
      onError(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  const requestSubmit = () => {
    if (!ready) return
    if (!paper && !liveAcknowledged) {
      onLiveConfirmRequested(() => setConfirmOpen(true))
      return
    }
    setConfirmOpen(true)
  }

  return (
    <div className="space-y-2 text-xs">
      {accounts.length > 1 && (
        <label
          className="flex items-center justify-between gap-2"
          title="IB account this order will route to. Persists per session."
        >
          <span className="text-[10px] uppercase tracking-wide text-neutral-500">
            Account
          </span>
          <select
            value={selectedAccount ?? ''}
            onChange={(e) => onSelectAccount(e.target.value || null)}
            className="flex-1 rounded border border-neutral-700 bg-neutral-900 px-2 py-0.5 text-xs num"
          >
            {accounts.map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
        </label>
      )}

      <div className="flex gap-1">
        <SideButton label="BUY" active={side === 'buy'} onClick={() => setSide('buy')} />
        <SideButton label="SELL" active={side === 'sell'} onClick={() => setSide('sell')} />
      </div>

      <HotButtons
        side={side}
        last={last}
        symbol={symbol}
        disabled={submitting || !last}
        onFire={fireHotOrder}
      />

      <Row label="Symbol" value={symbol} />
      <Row label="Last" value={fmtCurrency(last)} />

      {side === 'buy' && (
        <>
          <NumberInput
            label="Stop"
            placeholder="e.g. 122.50"
            value={stopPrice}
            onChange={setStopPrice}
            step={0.01}
          />
          <NumberInput
            label="Risk %"
            value={String(riskPct)}
            onChange={(v) => {
              const n = Number(v)
              if (Number.isFinite(n) && n > 0) setRiskPct(n)
            }}
            step={0.1}
            suffix="% of equity"
          />
          <Row label="Risk $" value={equity ? fmtCurrency(riskDollars) : '—'} />
        </>
      )}

      <NumberInput
        label="Qty (override)"
        placeholder={computed ? String(computed.qty) : 'auto'}
        value={overrideQty}
        onChange={setOverrideQty}
        step={1}
      />

      {computed && (
        <div className="rounded border border-neutral-800 bg-neutral-900/40 p-1.5">
          <Row label="Qty" value={fmtNumber(computed.qty)} mono />
          {computed.source === 'risk' && (
            <Row
              label="Risk / share"
              value={fmtCurrency(computed.riskPerShare)}
              mono
            />
          )}
          <Row
            label="Notional"
            value={fmtCurrency(computed.qty * last)}
            mono
          />
          {targets && (
            <div className="mt-1 border-t border-neutral-800 pt-1">
              {targets.map((t) => (
                <Row
                  key={t.r}
                  label={`${t.r}R target`}
                  value={fmtCurrency(t.price)}
                  mono
                />
              ))}
            </div>
          )}
        </div>
      )}

      <button
        type="button"
        onClick={requestSubmit}
        disabled={!ready}
        className={`w-full rounded px-3 py-1.5 text-xs font-bold ${
          side === 'buy'
            ? 'bg-[var(--color-accent)] text-neutral-950'
            : 'bg-[var(--color-danger)] text-white'
        } ${ready ? '' : 'cursor-not-allowed opacity-50'}`}
        title={ready ? `${side.toUpperCase()} ${qty} ${symbol}` : 'Enter a stop or qty'}
      >
        {side === 'buy' ? 'BUY' : 'SELL'} {qty || '—'} {symbol}
      </button>

      {!paper && (
        <p className="text-center text-[10px] uppercase tracking-wide text-[var(--color-danger)]">
          {liveAcknowledged ? 'live trading active' : 'live not yet confirmed'}
        </p>
      )}

      {confirmOpen && (
        <ConfirmOrderModal
          side={side}
          symbol={symbol}
          qty={qty}
          last={last}
          paper={paper}
          submitting={submitting}
          onCancel={() => setConfirmOpen(false)}
          onConfirm={sendOrder}
        />
      )}
    </div>
  )
}

function ConfirmOrderModal({
  side,
  symbol,
  qty,
  last,
  paper,
  submitting,
  onCancel,
  onConfirm,
}: {
  side: Side
  symbol: string
  qty: number
  last: number
  paper: boolean
  submitting: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div
        className={`w-full max-w-sm rounded-lg border p-4 shadow-2xl ${
          paper ? 'border-neutral-700 bg-neutral-950' : 'border-[var(--color-danger)] bg-neutral-950'
        }`}
      >
        <h2 className="mb-2 text-sm font-semibold">
          {paper ? 'Confirm Paper Order' : 'CONFIRM LIVE ORDER'}
        </h2>
        <div className="space-y-1 text-sm">
          <div className="flex justify-between">
            <span className="text-neutral-500">Side</span>
            <span
              className={
                side === 'buy'
                  ? 'font-bold text-[var(--color-accent)]'
                  : 'font-bold text-[var(--color-danger)]'
              }
            >
              {side.toUpperCase()}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-neutral-500">Symbol</span>
            <span className="font-semibold">{symbol}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-neutral-500">Qty</span>
            <span className="num">{fmtNumber(qty)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-neutral-500">Type</span>
            <span>Market · Day</span>
          </div>
          <div className="flex justify-between">
            <span className="text-neutral-500">Est. notional</span>
            <span className="num">{fmtCurrency(qty * last)}</span>
          </div>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className="rounded border border-neutral-700 px-3 py-1.5 text-xs disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={submitting}
            className={`rounded px-3 py-1.5 text-xs font-bold ${
              side === 'buy'
                ? 'bg-[var(--color-accent)] text-neutral-950'
                : 'bg-[var(--color-danger)] text-white'
            } disabled:opacity-50`}
          >
            {submitting ? 'Submitting…' : 'Confirm'}
          </button>
        </div>
      </div>
    </div>
  )
}

function SideButton({
  label,
  active,
  onClick,
}: {
  label: string
  active: boolean
  onClick: () => void
}) {
  const buyish = label === 'BUY'
  const baseColor = buyish ? 'var(--color-accent)' : 'var(--color-danger)'
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex-1 rounded px-2 py-1 text-xs font-semibold ${
        active ? 'text-neutral-950' : 'border border-neutral-700 text-neutral-300'
      }`}
      style={active ? { backgroundColor: baseColor } : {}}
    >
      {label}
    </button>
  )
}

function Row({
  label,
  value,
  mono = false,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-neutral-500">{label}</span>
      <span className={mono ? 'num' : ''}>{value}</span>
    </div>
  )
}

function round2(n: number): number {
  return Math.round(n * 100) / 100
}

/** Two rows of one-click order buttons, mirroring the active Side. The
 * `@Mkt` row sends a market order; the `@Bid+1t` / `@Ask-1t` row sends
 * a limit order priced at `last ± 1 tick` — an approximation pending
 * real-time L1 quote streaming.
 */
function HotButtons({
  side,
  last,
  symbol,
  disabled,
  onFire,
}: {
  side: Side
  last: number
  symbol: string
  disabled: boolean
  onFire: (side: Side, kind: HotKind, qty: number) => void
}) {
  const isBuy = side === 'buy'
  const capPrice = isBuy
    ? round2(last + PEG_CAP_OFFSET)
    : Math.max(0.01, round2(last - PEG_CAP_OFFSET))
  const limitLabel = isBuy ? '@Bid+1t' : '@Ask-1t'
  const colorClass = isBuy
    ? 'border-[var(--color-accent)]/60 text-[var(--color-accent)] hover:bg-[var(--color-accent)]/15'
    : 'border-[var(--color-danger)]/60 text-[var(--color-danger)] hover:bg-[var(--color-danger)]/15'

  return (
    <div className="space-y-1 rounded border border-neutral-800 bg-neutral-900/40 p-1.5">
      <HotRow
        rowLabel="@Mkt"
        rowTitle={`Market ${side.toUpperCase()} — fills immediately at the best price`}
        disabled={disabled}
        colorClass={colorClass}
        onClick={(qty) => onFire(side, 'mkt', qty)}
        buttonTitle={(qty) => `${side.toUpperCase()} ${qty} ${symbol} @ market`}
      />
      <HotRow
        rowLabel={limitLabel}
        rowTitle={
          last
            ? `Pegged-to-Primary ${side.toUpperCase()} — pegs to ` +
              `${isBuy ? 'best bid + 1 tick' : 'best ask − 1 tick'}; ` +
              `auto-adjusts as quote moves. Hard ${isBuy ? 'ceiling' : 'floor'} ` +
              `at ${capPrice.toFixed(2)} (${isBuy ? '+' : '−'}$${PEG_CAP_OFFSET.toFixed(2)} from last).`
            : 'Last price unknown — select a ticker first'
        }
        disabled={disabled}
        colorClass={colorClass}
        onClick={(qty) => onFire(side, 'bid_plus_tick', qty)}
        buttonTitle={(qty) =>
          `${side.toUpperCase()} ${qty} ${symbol} @ pegged ` +
          `(cap ${capPrice.toFixed(2)})`
        }
      />
    </div>
  )
}

function HotRow({
  rowLabel,
  rowTitle,
  disabled,
  colorClass,
  onClick,
  buttonTitle,
}: {
  rowLabel: string
  rowTitle: string
  disabled: boolean
  colorClass: string
  onClick: (qty: number) => void
  buttonTitle: (qty: number) => string
}) {
  return (
    <div className="flex items-center gap-1">
      <span
        className="w-14 shrink-0 text-[10px] uppercase tracking-wide text-neutral-500"
        title={rowTitle}
      >
        {rowLabel}
      </span>
      <div className="flex flex-1 gap-1">
        {HOT_QTYS.map((qty) => (
          <button
            key={qty}
            type="button"
            disabled={disabled}
            onClick={() => onClick(qty)}
            title={buttonTitle(qty)}
            className={`flex-1 rounded border px-0 py-0.5 text-[10px] font-semibold ${colorClass} disabled:cursor-not-allowed disabled:opacity-40`}
          >
            {qty}
          </button>
        ))}
      </div>
    </div>
  )
}

function NumberInput({
  label,
  value,
  onChange,
  placeholder,
  step,
  suffix,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  step?: number
  suffix?: string
}) {
  return (
    <label className="flex items-center justify-between gap-2">
      <span className="text-neutral-500">{label}</span>
      <span className="flex items-center gap-1">
        <input
          type="number"
          inputMode="decimal"
          value={value}
          placeholder={placeholder}
          step={step}
          onChange={(e) => onChange(e.target.value)}
          className="w-20 rounded border border-neutral-700 bg-neutral-900 px-1.5 py-0.5 text-right num"
        />
        {suffix && <span className="text-[10px] text-neutral-500">{suffix}</span>}
      </span>
    </label>
  )
}
