import { useEffect, useRef, useState } from 'react'

interface Props {
  onApprove: () => void
  onCancel: () => void
}

const REQUIRED = 'LIVE'

/**
 * Session-scoped acknowledgement modal. Shown the first time the user
 * tries to place an order or close a position while the API is in live
 * mode. They must type "LIVE" exactly to proceed; once they do, the rest
 * of the session uses the regular per-order confirmation only.
 */
export function LiveConfirmModal({ onApprove, onCancel }: Props) {
  const [typed, setTyped] = useState('')
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const matches = typed.trim() === REQUIRED

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="w-full max-w-sm rounded-lg border-2 border-[var(--color-danger)] bg-neutral-950 p-4 shadow-2xl">
        <h2 className="mb-2 text-base font-semibold text-[var(--color-danger)]">
          ⚠ LIVE Trading
        </h2>
        <p className="mb-3 text-sm text-neutral-200">
          You are about to place an order on a <strong>real-money</strong> IBKR account.
          Bugs, fat fingers, and stale data become real losses.
        </p>
        <p className="mb-2 text-xs text-neutral-400">
          Type <code className="rounded bg-neutral-800 px-1 font-bold">{REQUIRED}</code> to
          confirm and unlock the session. You'll still confirm each individual order, but
          this acknowledgement won't be asked again until you reload.
        </p>
        <input
          ref={inputRef}
          type="text"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={REQUIRED}
          autoComplete="off"
          spellCheck={false}
          className="w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm font-mono"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && matches) onApprove()
            if (e.key === 'Escape') onCancel()
          }}
        />
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded border border-neutral-700 px-3 py-1.5 text-xs"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onApprove}
            disabled={!matches}
            className="rounded bg-[var(--color-danger)] px-3 py-1.5 text-xs font-bold text-white disabled:opacity-50"
          >
            Acknowledge & Continue
          </button>
        </div>
      </div>
    </div>
  )
}
