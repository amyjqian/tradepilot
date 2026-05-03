import { useEffect, useState } from 'react'
import { cancelOrdersForSymbol, closePosition, submitOrder } from '../api'
import type { BrokerPosition, OrderRecord, ScanResult } from '../types'

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

const HOT_QTY_PRESETS = [10, 25, 50, 100, 500] as const
const DEFAULT_HOT_QTY = 10
const HOT_QTY_KEY = 'tradepilot.hot_qty'

// Distance presets (dollars from last) for the Take Profit / Stop Loss
// rows. Reasonable values for liquid names; user can override via the
// `Dist` input. Keep this short — the row's already crowded.
const DIST_PRESETS = [0.1, 0.4, 0.8] as const

type Side = 'buy' | 'sell'
/** Ladder of price-aggression options behind each hot button. Wired to:
 *   mkt          → MarketOrder
 *   mid          → IB MIDPRICE (NBBO midpoint, capped)
 *   bid_or_ask   → REL with peg_offset=0  (joins the bid/ask queue)
 *   bid_plus_tick→ REL with peg_offset=$0.01 (one tick past the queue,
 *                  so the order sits at the front but doesn't cross)
 */
type HotKind = 'mkt' | 'mid' | 'bid_or_ask' | 'bid_plus_tick'

interface Props {
  selected: ScanResult | null
  paper: boolean
  /** All managed IB accounts visible from the connection. */
  accounts: string[]
  /** Currently-selected routing account, or null to use broker default. */
  selectedAccount: string | null
  onSelectAccount: (acct: string | null) => void
  liveAcknowledged: boolean
  onLiveConfirmRequested: (onApprove: () => void) => void
  /** Live broker positions — used to enable Flatten/Reverse/TP/SL only
   * when there's an open position in the selected ticker, and to size
   * those operations off the actual position. */
  positions: BrokerPosition[]
  /** Orders the broker still considers working — used to enable the
   * Cancel button only when there's something to cancel. */
  workingOrders: OrderRecord[]
  onAfterOrder: () => void
  onError: (msg: string) => void
}

export function OrderTicket({
  selected,
  paper,
  accounts,
  selectedAccount,
  onSelectAccount,
  liveAcknowledged,
  onLiveConfirmRequested,
  positions,
  workingOrders,
  onAfterOrder,
  onError,
}: Props) {
  const [submitting, setSubmitting] = useState(false)
  // Hot-button qty: persisted in localStorage so reloads keep your size.
  const [hotQty, setHotQty] = useState<number>(() => {
    const saved = Number(window.localStorage.getItem(HOT_QTY_KEY))
    return Number.isFinite(saved) && saved > 0 ? saved : DEFAULT_HOT_QTY
  })
  useEffect(() => {
    window.localStorage.setItem(HOT_QTY_KEY, String(hotQty))
  }, [hotQty])

  if (!selected) {
    return (
      <p className="text-xs text-neutral-500">Select a ticker to load the ticket.</p>
    )
  }

  const last = selected.price ?? 0
  const symbol = selected.ticker
  const position = positions.find((p) => p.symbol === symbol) ?? null
  const positionQty = position?.qty ?? 0
  const hasPosition = positionQty !== 0
  const isLong = positionQty > 0
  // Working orders for this symbol — disables Cancel when nothing to do.
  const symbolWorkingCount = workingOrders.filter((o) => o.symbol === symbol).length

  /** All actions route through this so the live-confirm gate is enforced
   * exactly once. The action itself (the inner async fn) runs after the
   * gate clears on live, or immediately on paper. */
  const guarded = (run: () => Promise<void>) => {
    if (!paper && !liveAcknowledged) {
      onLiveConfirmRequested(() => void run())
      return
    }
    void run()
  }

  const wrapSubmit = async (run: () => Promise<unknown>) => {
    setSubmitting(true)
    try {
      await run()
      onAfterOrder()
    } catch (e) {
      onError(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  // -----------------------------------------------------------------
  // Hot buttons (Buy/Sell @ MKT / Mid / Bid|Ask / Bid|Ask ±1t)
  // -----------------------------------------------------------------

  const fireHotOrder = (hotSide: Side, kind: HotKind) => {
    if (!last) {
      onError('Cannot place order: last price unknown')
      return
    }
    guarded(() => doHotOrder(hotSide, kind))
  }

  const doHotOrder = (hotSide: Side, kind: HotKind) =>
    wrapSubmit(async () => {
      const score_at_entry =
        Number.isFinite(selected.score) && selected.score !== 0
          ? selected.score
          : undefined
      // Cap = worst acceptable price = last ± $0.50. Used as `lmtPrice`
      // for both REL (peg-to-primary) and MIDPRICE — IB requires it as
      // a runaway-tape stop. SELLs can't go below $0.01 (penny floor).
      const cap_price =
        hotSide === 'buy'
          ? round2(last + PEG_CAP_OFFSET)
          : Math.max(0.01, round2(last - PEG_CAP_OFFSET))

      const orderParams = (() => {
        switch (kind) {
          case 'mkt':
            return { type: 'market' as const }
          case 'mid':
            return { type: 'midprice' as const, cap_price }
          case 'bid_or_ask':
            return { type: 'pegprim' as const, peg_offset: 0, cap_price }
          case 'bid_plus_tick':
            return { type: 'pegprim' as const, peg_offset: TICK_SIZE, cap_price }
        }
      })()

      await submitOrder({
        symbol,
        qty: hotQty,
        side: hotSide,
        time_in_force: 'day',
        ...orderParams,
        ...(score_at_entry !== undefined ? { score_at_entry } : {}),
        ...(selectedAccount ? { account: selectedAccount } : {}),
      })
    })

  // -----------------------------------------------------------------
  // Position management (Flatten / 25% / 50% / 75% / Reverse / Cancel)
  // -----------------------------------------------------------------

  const fireClosePct = (percentage: number) => {
    if (!hasPosition) return
    guarded(() =>
      wrapSubmit(() =>
        closePosition(symbol, {
          percentage,
          account: selectedAccount ?? undefined,
        }),
      ),
    )
  }

  const fireFlatten = () => fireClosePct(100)

  /** Reverse the current position: submit a market order in the opposite
   * direction sized at 2× the current absolute qty so the resulting net
   * is exactly the mirror image. One order, no race window. */
  const fireReverse = () => {
    if (!hasPosition) return
    const qty = Math.abs(positionQty) * 2
    const reverseSide: Side = isLong ? 'sell' : 'buy'
    guarded(() =>
      wrapSubmit(() =>
        submitOrder({
          symbol,
          qty,
          side: reverseSide,
          type: 'market',
          time_in_force: 'day',
          ...(selectedAccount ? { account: selectedAccount } : {}),
        }),
      ),
    )
  }

  const fireCancel = () => {
    if (symbolWorkingCount === 0) return
    guarded(() => wrapSubmit(() => cancelOrdersForSymbol(symbol)))
  }

  // -----------------------------------------------------------------
  // Take Profit / Stop Loss with distance from `last`
  // -----------------------------------------------------------------

  const fireTakeProfit = (dist: number) => {
    if (!hasPosition || !last || !(dist > 0)) return
    // Exit side opposite the position; price moves favorably by `dist`.
    const exitSide: Side = isLong ? 'sell' : 'buy'
    const limit_price = isLong ? round2(last + dist) : Math.max(0.01, round2(last - dist))
    const qty = Math.abs(positionQty)
    guarded(() =>
      wrapSubmit(() =>
        submitOrder({
          symbol,
          qty,
          side: exitSide,
          type: 'limit',
          time_in_force: 'gtc',
          limit_price,
          ...(selectedAccount ? { account: selectedAccount } : {}),
        }),
      ),
    )
  }

  const fireStopLoss = (dist: number) => {
    if (!hasPosition || !last || !(dist > 0)) return
    // Exit side opposite the position; stop trigger is unfavorable by `dist`.
    const exitSide: Side = isLong ? 'sell' : 'buy'
    const stop_price = isLong ? Math.max(0.01, round2(last - dist)) : round2(last + dist)
    const qty = Math.abs(positionQty)
    guarded(() =>
      wrapSubmit(() =>
        submitOrder({
          symbol,
          qty,
          side: exitSide,
          type: 'stop',
          time_in_force: 'gtc',
          stop_price,
          ...(selectedAccount ? { account: selectedAccount } : {}),
        }),
      ),
    )
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

      <QtySelector value={hotQty} onChange={setHotQty} />

      {/* 4 rows of Buy / Sell pairs — left col green (buy), right col red (sell). */}
      <ActionGrid
        qty={hotQty}
        symbol={symbol}
        disabled={submitting || !last}
        onFire={fireHotOrder}
      />

      {/* Flatten / Cancel / 25% / 50% / 75% / Reverse — 3 rows of 2. */}
      <PositionMgmtGrid
        symbol={symbol}
        hasPosition={hasPosition}
        positionQty={positionQty}
        cancelDisabled={symbolWorkingCount === 0}
        cancelCount={symbolWorkingCount}
        disabled={submitting}
        onFlatten={fireFlatten}
        onCancel={fireCancel}
        onReverse={fireReverse}
        onClosePct={fireClosePct}
      />

      <DistRow
        label="Take Profit"
        accent="buy"
        disabled={submitting || !hasPosition || !last}
        onFire={fireTakeProfit}
      />
      <DistRow
        label="Stop Loss"
        accent="sell"
        disabled={submitting || !hasPosition || !last}
        onFire={fireStopLoss}
      />

      {!paper && (
        <p className="text-center text-[10px] uppercase tracking-wide text-[var(--color-danger)]">
          {liveAcknowledged ? 'live trading active' : 'live not yet confirmed'}
        </p>
      )}
    </div>
  )
}

function round2(n: number): number {
  return Math.round(n * 100) / 100
}

// ---------------------------------------------------------------------
// Quantity selector — preset chips + custom input. The selected qty
// applies to all the Buy/Sell action buttons in the row below.
// ---------------------------------------------------------------------

function QtySelector({
  value,
  onChange,
}: {
  value: number
  onChange: (n: number) => void
}) {
  const [custom, setCustom] = useState<string>(() =>
    HOT_QTY_PRESETS.includes(value as (typeof HOT_QTY_PRESETS)[number])
      ? ''
      : String(value),
  )

  useEffect(() => {
    const isPreset = HOT_QTY_PRESETS.includes(
      value as (typeof HOT_QTY_PRESETS)[number],
    )
    setCustom(isPreset ? '' : String(value))
  }, [value])

  const commitCustom = (raw: string) => {
    setCustom(raw)
    const n = Math.floor(Number(raw))
    if (Number.isFinite(n) && n > 0) onChange(n)
  }

  return (
    <div className="flex items-center gap-1">
      {HOT_QTY_PRESETS.map((q) => {
        const active = value === q && !custom
        return (
          <button
            key={q}
            type="button"
            onClick={() => {
              setCustom('')
              onChange(q)
            }}
            className={`flex-1 rounded px-1 py-1 num text-xs font-semibold transition-colors ${
              active
                ? 'bg-[var(--color-accent)] text-neutral-950'
                : 'border border-neutral-700 bg-neutral-900 text-neutral-300 hover:border-neutral-500'
            }`}
            title={`Set hot-button qty to ${q}`}
          >
            {q}
          </button>
        )
      })}
      <input
        type="number"
        inputMode="numeric"
        min={1}
        step={1}
        value={custom}
        placeholder="Qty"
        onChange={(e) => commitCustom(e.target.value)}
        className="w-14 rounded border border-neutral-700 bg-neutral-900 px-1.5 py-1 text-right num text-xs text-neutral-200 placeholder:text-neutral-600"
        title="Custom hot-button qty"
      />
    </div>
  )
}

// ---------------------------------------------------------------------
// Buy / Sell action grid — 4 rows × 2 cols.
// ---------------------------------------------------------------------

const HOT_ROWS: {
  kind: HotKind
  buyLabel: string
  sellLabel: string
  buyTip: string
  sellTip: string
}[] = [
  {
    kind: 'mkt',
    buyLabel: '@MKT',
    sellLabel: '@MKT',
    buyTip: 'Market — fills immediately at best ask',
    sellTip: 'Market — fills immediately at best bid',
  },
  {
    kind: 'mid',
    buyLabel: '@Mid',
    sellLabel: '@Mid',
    buyTip: 'IB MIDPRICE — pegs to NBBO midpoint, capped at last + $0.50',
    sellTip: 'IB MIDPRICE — pegs to NBBO midpoint, capped at last − $0.50',
  },
  {
    kind: 'bid_or_ask',
    buyLabel: '@Bid Limit',
    sellLabel: '@Ask Limit',
    buyTip: 'Pegs to best bid (joins the queue, most passive)',
    sellTip: 'Pegs to best ask (joins the queue, most passive)',
  },
  {
    kind: 'bid_plus_tick',
    buyLabel: '@Bid +1t',
    sellLabel: '@Ask -1t',
    buyTip: 'Pegs to best bid + 1 tick (sits at the inside)',
    sellTip: 'Pegs to best ask − 1 tick (sits at the inside)',
  },
]

function ActionGrid({
  qty,
  symbol,
  disabled,
  onFire,
}: {
  qty: number
  symbol: string
  disabled: boolean
  onFire: (side: Side, kind: HotKind) => void
}) {
  return (
    <div className="grid grid-cols-2 gap-1">
      {HOT_ROWS.map((row) => (
        <FragmentRow key={row.kind}>
          <ActionButton
            side="buy"
            qty={qty}
            symbol={symbol}
            label={row.buyLabel}
            tip={row.buyTip}
            disabled={disabled}
            onClick={() => onFire('buy', row.kind)}
          />
          <ActionButton
            side="sell"
            qty={qty}
            symbol={symbol}
            label={row.sellLabel}
            tip={row.sellTip}
            disabled={disabled}
            onClick={() => onFire('sell', row.kind)}
          />
        </FragmentRow>
      ))}
    </div>
  )
}

function FragmentRow({ children }: { children: React.ReactNode }) {
  return <>{children}</>
}

function ActionButton({
  side,
  qty,
  symbol,
  label,
  tip,
  disabled,
  onClick,
}: {
  side: Side
  qty: number
  symbol: string
  label: string
  tip: string
  disabled: boolean
  onClick: () => void
}) {
  const isBuy = side === 'buy'
  const verb = isBuy ? 'Buy' : 'Sell'
  const color = isBuy
    ? 'bg-[var(--color-accent)] text-neutral-950 hover:bg-[var(--color-accent)]/90'
    : 'bg-[var(--color-danger)] text-white hover:bg-[var(--color-danger)]/90'
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      title={`${verb} ${qty} ${symbol} ${label} — ${tip}`}
      className={`rounded px-1 py-1.5 text-[11px] font-bold leading-tight whitespace-nowrap ${color} disabled:cursor-not-allowed disabled:opacity-40`}
    >
      {verb} {qty} {label}
    </button>
  )
}

// ---------------------------------------------------------------------
// Position management — Flatten/Cancel, 25%/50%, Reverse/75%.
// ---------------------------------------------------------------------

function PositionMgmtGrid({
  symbol,
  hasPosition,
  positionQty,
  cancelDisabled,
  cancelCount,
  disabled,
  onFlatten,
  onCancel,
  onReverse,
  onClosePct,
}: {
  symbol: string
  hasPosition: boolean
  positionQty: number
  cancelDisabled: boolean
  cancelCount: number
  disabled: boolean
  onFlatten: () => void
  onCancel: () => void
  onReverse: () => void
  onClosePct: (pct: number) => void
}) {
  const posLabel = hasPosition
    ? `${positionQty > 0 ? '+' : ''}${positionQty} ${symbol}`
    : `no position in ${symbol}`
  return (
    <div className="grid grid-cols-2 gap-1">
      <MgmtButton
        label="Flatten"
        tip={`Close 100% of ${posLabel} at market`}
        bg="bg-amber-700 hover:bg-amber-600"
        disabled={disabled || !hasPosition}
        onClick={onFlatten}
      />
      <MgmtButton
        label={cancelCount > 0 ? `Cancel (${cancelCount})` : 'Cancel'}
        tip={
          cancelDisabled
            ? `No working orders for ${symbol}`
            : `Cancel ${cancelCount} working order${cancelCount === 1 ? '' : 's'} for ${symbol}`
        }
        bg="bg-neutral-700 hover:bg-neutral-600"
        disabled={disabled || cancelDisabled}
        onClick={onCancel}
      />
      <MgmtButton
        label="25%"
        tip={`Close 25% of ${posLabel}`}
        bg="bg-neutral-500 hover:bg-neutral-400 text-neutral-950"
        disabled={disabled || !hasPosition}
        onClick={() => onClosePct(25)}
      />
      <MgmtButton
        label="50%"
        tip={`Close 50% of ${posLabel}`}
        bg="bg-neutral-500 hover:bg-neutral-400 text-neutral-950"
        disabled={disabled || !hasPosition}
        onClick={() => onClosePct(50)}
      />
      <MgmtButton
        label="Reverse"
        tip={
          hasPosition
            ? `Flip ${posLabel} → ${positionQty > 0 ? 'short' : 'long'} ${Math.abs(positionQty)} (one market order, 2× qty)`
            : `No position in ${symbol} to reverse`
        }
        bg="bg-purple-700 hover:bg-purple-600"
        disabled={disabled || !hasPosition}
        onClick={onReverse}
      />
      <MgmtButton
        label="75%"
        tip={`Close 75% of ${posLabel}`}
        bg="bg-neutral-500 hover:bg-neutral-400 text-neutral-950"
        disabled={disabled || !hasPosition}
        onClick={() => onClosePct(75)}
      />
    </div>
  )
}

function MgmtButton({
  label,
  tip,
  bg,
  disabled,
  onClick,
}: {
  label: string
  tip: string
  bg: string
  disabled: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      title={tip}
      className={`rounded px-1 py-1.5 text-[11px] font-semibold text-white whitespace-nowrap ${bg} disabled:cursor-not-allowed disabled:opacity-40`}
    >
      {label}
    </button>
  )
}

// ---------------------------------------------------------------------
// Take Profit / Stop Loss row — 3 distance presets + custom input + button.
// `dist` is interpreted as dollars from `last`; the row computes the
// limit / stop price using the current position direction.
// ---------------------------------------------------------------------

function DistRow({
  label,
  accent,
  disabled,
  onFire,
}: {
  label: string
  accent: Side
  disabled: boolean
  onFire: (dist: number) => void
}) {
  const [custom, setCustom] = useState<string>('')
  const [activePreset, setActivePreset] = useState<number | null>(null)

  const customDist = Number(custom)
  const submitDist = () => {
    if (activePreset !== null) onFire(activePreset)
    else if (Number.isFinite(customDist) && customDist > 0) onFire(customDist)
  }

  const accentClass =
    accent === 'buy'
      ? 'bg-[var(--color-accent)] text-neutral-950 hover:bg-[var(--color-accent)]/90'
      : 'bg-[var(--color-danger)] text-white hover:bg-[var(--color-danger)]/90'

  const ready =
    !disabled &&
    (activePreset !== null || (Number.isFinite(customDist) && customDist > 0))

  return (
    <div className="flex items-center gap-1">
      {DIST_PRESETS.map((d) => {
        const active = activePreset === d
        return (
          <button
            key={d}
            type="button"
            disabled={disabled}
            onClick={() => {
              setCustom('')
              setActivePreset(d)
            }}
            className={`w-10 shrink-0 rounded px-1 py-1 num text-[11px] font-semibold transition-colors ${
              active
                ? 'bg-[var(--color-accent)] text-neutral-950'
                : 'border border-neutral-700 bg-neutral-900 text-neutral-300 hover:border-neutral-500'
            } disabled:cursor-not-allowed disabled:opacity-40`}
            title={`${label} at $${d.toFixed(2)} from last`}
          >
            {d}
          </button>
        )
      })}
      <input
        type="number"
        inputMode="decimal"
        min={0}
        step={0.01}
        value={custom}
        placeholder="Dist"
        onChange={(e) => {
          setCustom(e.target.value)
          setActivePreset(null)
        }}
        className="min-w-0 flex-1 rounded border border-neutral-700 bg-neutral-900 px-1.5 py-1 text-right num text-xs text-neutral-200 placeholder:text-neutral-600"
        title={`Custom ${label.toLowerCase()} distance ($ from last)`}
      />
      <button
        type="button"
        disabled={!ready}
        onClick={submitDist}
        className={`shrink-0 rounded px-2 py-1 text-[11px] font-bold ${accentClass} disabled:cursor-not-allowed disabled:opacity-40`}
        title={
          disabled
            ? `${label} requires an open position in this symbol`
            : `Submit ${label} order`
        }
      >
        {label}
      </button>
    </div>
  )
}
