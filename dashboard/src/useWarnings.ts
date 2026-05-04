import { useCallback, useEffect, useRef, useState } from 'react'
import { BASE_URL } from './api'

export interface AppWarning {
  kind: string
  message: string
  /** Unix epoch in seconds (server clock). */
  ts: number
  detail?: Record<string, unknown>
}

interface ToastWarning extends AppWarning {
  /** Client-assigned id, stable across renders, used for keying toasts. */
  id: number
}

interface UseWarnings {
  /** Toasts the UI should currently show — most-recent-first, capped. */
  toasts: ToastWarning[]
  /** Hide one specific toast (manual dismiss). */
  dismiss: (id: number) => void
  /** Hide all toasts at once. */
  clear: () => void
}

const RECONNECT_DELAY_MS = 3_000

/**
 * Subscribes to `/warnings/stream` (SSE) and exposes a list of recent
 * warnings as toasts. Auto-dismisses each toast after `lifetimeMs`
 * unless the user clicks it. Survives panel switches because it lives
 * in App-level state.
 *
 * The initial `snapshot` from the server is intentionally NOT shown
 * as toasts — those are old and would confuse the user. Only events
 * that arrive *after* the SSE is open get toasted.
 */
export function useWarnings(
  lifetimeMs: number = 10_000,
  maxVisible: number = 5,
): UseWarnings {
  const [toasts, setToasts] = useState<ToastWarning[]>([])
  const idRef = useRef<number>(1)
  const sourceRef = useRef<EventSource | null>(null)
  const reconnectRef = useRef<number | null>(null)
  const cancelledRef = useRef(false)

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const clear = useCallback(() => setToasts([]), [])

  useEffect(() => {
    cancelledRef.current = false
    const open = () => {
      if (cancelledRef.current) return
      const es = new EventSource(`${BASE_URL}/warnings/stream`)
      sourceRef.current = es

      // Snapshot is informational only — don't burst-toast the user
      // with potentially stale warnings on every reconnect.
      es.addEventListener('snapshot', () => {
        // intentionally empty
      })

      es.onmessage = (ev) => {
        try {
          const w = JSON.parse(ev.data) as AppWarning
          if (!w || typeof w.message !== 'string') return
          const id = idRef.current++
          setToasts((prev) => [{ ...w, id }, ...prev].slice(0, maxVisible))
          // Auto-dismiss after `lifetimeMs`. Click-to-dismiss handled
          // by the toast UI; this is the fallback so old warnings
          // don't pile up forever.
          window.setTimeout(() => {
            setToasts((prev) => prev.filter((t) => t.id !== id))
          }, lifetimeMs)
        } catch {
          // Heartbeats are SSE comment lines (`:` prefix); they don't
          // fire onmessage. Anything that does is a parse failure
          // worth ignoring rather than surfacing.
        }
      }

      es.onerror = () => {
        es.close()
        sourceRef.current = null
        if (cancelledRef.current) return
        reconnectRef.current = window.setTimeout(open, RECONNECT_DELAY_MS)
      }
    }
    open()
    return () => {
      cancelledRef.current = true
      if (reconnectRef.current !== null) {
        window.clearTimeout(reconnectRef.current)
        reconnectRef.current = null
      }
      sourceRef.current?.close()
      sourceRef.current = null
    }
  }, [lifetimeMs, maxVisible])

  return { toasts, dismiss, clear }
}
