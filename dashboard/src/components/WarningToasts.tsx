import { useWarnings } from '../useWarnings'

const KIND_LABEL: Record<string, string> = {
  polygon_rate_limit: 'Polygon rate limit',
  ib_pacing: 'IB pacing',
}

function fmtTime(epochSec: number): string {
  const d = new Date(epochSec * 1000)
  return d.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

/** Bottom-right stack of dismissible toasts driven by the
 * `/warnings/stream` SSE. Lives at app root so it's visible from
 * every tab. Click a toast to dismiss; otherwise each fades after
 * 10s. */
export function WarningToasts() {
  const { toasts, dismiss } = useWarnings()
  if (toasts.length === 0) return null
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-40 flex w-80 flex-col gap-1.5">
      {toasts.map((t) => (
        <button
          key={t.id}
          type="button"
          onClick={() => dismiss(t.id)}
          className="pointer-events-auto flex flex-col gap-0.5 rounded border border-[var(--color-warn)]/60 bg-neutral-950/95 p-2 text-left text-xs shadow-lg backdrop-blur-sm hover:border-[var(--color-warn)]"
          title="Click to dismiss"
        >
          <span className="flex items-center justify-between gap-2">
            <span className="text-[10px] font-bold uppercase tracking-wide text-[var(--color-warn)]">
              {KIND_LABEL[t.kind] ?? t.kind}
            </span>
            <span className="num text-[10px] text-neutral-500">
              {fmtTime(t.ts)}
            </span>
          </span>
          <span className="text-neutral-200">{t.message}</span>
        </button>
      ))}
    </div>
  )
}
