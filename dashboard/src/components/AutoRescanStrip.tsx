import type { CountdownState } from '../useCountdown'

/** Status strip rendered under a scanner's toolbar when auto-rescan is
 * enabled. Shows the cadence, a prominent countdown to the next run, a
 * progress bar that fills over the cycle, and the start/end wall-clock
 * times of the most recent run. Shared by SectorRotationPanel and
 * WatchlistPanel — they have identical UX needs here. */
export function AutoRescanStrip({
  rescanMin,
  loading,
  countdown,
  runStartedAt,
  runEndedAt,
}: {
  rescanMin: number
  loading: boolean
  countdown: CountdownState
  runStartedAt: Date | null
  runEndedAt: Date | null
}) {
  const elapsedSec =
    runStartedAt && runEndedAt
      ? (runEndedAt.getTime() - runStartedAt.getTime()) / 1000
      : null

  return (
    <div className="space-y-1 rounded border border-neutral-800 bg-neutral-900/40 p-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] uppercase tracking-wide text-neutral-500">
          Auto every {rescanMin}m · aligned :+5s
        </span>
        {loading ? (
          <span className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-[var(--color-accent-dim)]">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--color-accent)]" />
            scanning
          </span>
        ) : countdown.text ? (
          <span
            className="num text-sm font-bold tabular-nums text-[var(--color-accent-dim)]"
            title="Time until the next aligned auto-rescan fires (boundary + 5s)"
          >
            {countdown.text}
          </span>
        ) : null}
      </div>

      {/* Progress bar — fills as we approach next run. Falls back to a
       * dim "indeterminate" bar before the first scan completes (no
       * `lastRunAt` yet, so we can't compute progress). */}
      <div className="h-1 overflow-hidden rounded bg-neutral-800">
        <div
          className="h-full bg-[var(--color-accent)] transition-[width] duration-1000 ease-linear"
          style={{ width: `${countdown.progressPct}%` }}
        />
      </div>

      {(runStartedAt || runEndedAt) && (
        <div className="flex justify-between text-[10px] text-neutral-500">
          <span>{runStartedAt ? `start ${fmtTime(runStartedAt)}` : ''}</span>
          <span>
            {runEndedAt
              ? `end ${fmtTime(runEndedAt)}${
                  elapsedSec != null ? ` (${elapsedSec.toFixed(1)}s)` : ''
                }`
              : runStartedAt
                ? '…'
                : ''}
          </span>
        </div>
      )}
    </div>
  )
}

function fmtTime(d: Date): string {
  return d.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}
