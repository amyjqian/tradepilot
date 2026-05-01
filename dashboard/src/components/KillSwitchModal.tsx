import { useState } from 'react'
import { closeAllPositions } from '../api'
import type { BrokerPosition } from '../types'
import { fmtCurrency } from '../format'

interface Props {
  positions: BrokerPosition[]
  paper: boolean
  onClose: () => void
  onAfterClose: () => void
  onError: (msg: string) => void
}

export function KillSwitchModal({ positions, paper, onClose, onAfterClose, onError }: Props) {
  const [submitting, setSubmitting] = useState(false)
  const [done, setDone] = useState<string | null>(null)

  const totalNotional = positions.reduce((s, p) => s + Math.abs(p.market_value), 0)

  const submit = async () => {
    setSubmitting(true)
    try {
      const res = await closeAllPositions()
      setDone(`Submitted ${res.submitted} order(s); ${res.ok} ok, ${res.failed} failed.`)
      onAfterClose()
    } catch (e) {
      onError(String(e))
      onClose()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-lg border border-[var(--color-danger)] bg-neutral-950 p-4 shadow-2xl">
        <h2 className="mb-2 text-base font-semibold text-[var(--color-danger)]">
          Kill Switch — Close All Positions
        </h2>
        <p className="mb-3 text-sm text-neutral-300">
          {paper ? 'Paper trading.' : 'LIVE TRADING.'} This will submit market close orders for
          every open position immediately and cancel all open orders.
        </p>

        {positions.length === 0 ? (
          <p className="text-sm text-neutral-500">No open positions to close.</p>
        ) : (
          <>
            <div className="mb-2 flex items-center justify-between text-xs">
              <span className="text-neutral-400">{positions.length} position(s)</span>
              <span className="num text-neutral-300">{fmtCurrency(totalNotional)} notional</span>
            </div>
            <ul className="mb-4 max-h-48 divide-y divide-neutral-800 overflow-y-auto rounded border border-neutral-800 bg-neutral-900/50">
              {positions.map((p) => (
                <li
                  key={p.symbol}
                  className="flex items-center justify-between px-2 py-1 text-xs"
                >
                  <span className="font-semibold">{p.symbol}</span>
                  <span className="num text-neutral-400">
                    {p.qty} @ {fmtCurrency(p.avg_entry_price)}
                  </span>
                  <span
                    className={`num ${
                      p.unrealized_pl_abs >= 0
                        ? 'text-[var(--color-accent-dim)]'
                        : 'text-[var(--color-danger)]'
                    }`}
                  >
                    {fmtCurrency(p.unrealized_pl_abs)}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}

        {done && <p className="mb-3 text-xs text-[var(--color-accent-dim)]">{done}</p>}

        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="rounded border border-neutral-700 px-3 py-1.5 text-xs disabled:opacity-50"
          >
            {done ? 'Close' : 'Cancel'}
          </button>
          {!done && (
            <button
              type="button"
              onClick={submit}
              disabled={submitting || positions.length === 0}
              className="rounded bg-[var(--color-danger)] px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
            >
              {submitting ? 'Submitting…' : 'Confirm Close All'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
