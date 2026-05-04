import { useEffect, useState } from 'react'

export interface CountdownState {
  /** "m:ss" string until `nextRunAt`, or null when disabled / unknown. */
  text: string | null
  remainingMs: number
  /** 0-100 — how much of the cycle has elapsed since `lastRunAt`. Used
   * to drive the visual progress bar. Returns 0 when we don't have both
   * endpoints yet (first cycle before any scan completes). */
  progressPct: number
}

/** Live countdown to `nextRunAt` plus a percentage of the cycle that has
 * elapsed since `lastRunAt`. Ticks once per second only while `enabled`
 * is true so disabled panels don't pay for the timer.
 */
export function useCountdown(
  lastRunAt: Date | null,
  nextRunAt: Date | null,
  enabled: boolean,
): CountdownState {
  const [now, setNow] = useState<number>(() => Date.now())
  useEffect(() => {
    if (!enabled || nextRunAt === null) return
    const id = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(id)
  }, [enabled, nextRunAt])

  if (!enabled || nextRunAt === null) {
    return { text: null, remainingMs: 0, progressPct: 0 }
  }
  const remainingMs = Math.max(0, nextRunAt.getTime() - now)
  const totalSec = Math.ceil(remainingMs / 1000)
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  const text = remainingMs <= 0 ? '0:00' : `${m}:${s.toString().padStart(2, '0')}`

  let progressPct = 0
  if (lastRunAt !== null) {
    const totalMs = nextRunAt.getTime() - lastRunAt.getTime()
    if (totalMs > 0) {
      const elapsed = totalMs - remainingMs
      progressPct = Math.min(100, Math.max(0, (elapsed / totalMs) * 100))
    }
  }
  return { text, remainingMs, progressPct }
}
