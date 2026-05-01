import { useCallback, useEffect, useRef, useState } from 'react'
import {
  BASE_URL,
  fetchBrokerStatus,
  fetchRiskStatus,
  resetRisk,
} from './api'
import type {
  AccountSnapshot,
  BrokerPosition,
  BrokerSnapshot,
  BrokerStatus,
  OrderRecord,
  RiskStatus,
} from './types'

interface BrokerData {
  status: BrokerStatus | null
  account: AccountSnapshot | null
  positions: BrokerPosition[]
  orders: OrderRecord[]
  risk: RiskStatus | null
  /** All managed accounts visible from the IBKR connection. */
  accounts: string[]
  /** Account the user has selected for routing orders. Defaults to the
   * broker's `default_account`; persisted in localStorage. */
  selectedAccount: string | null
  setSelectedAccount: (acct: string | null) => void
  error: string | null
  refresh: () => Promise<void>
  resetKillSwitch: () => Promise<void>
  /** True after the user has explicitly acknowledged live trading this session. */
  liveAcknowledged: boolean
  acknowledgeLive: () => void
}

type StreamEvent =
  | { kind: 'account'; payload: AccountSnapshot }
  | { kind: 'position'; payload: BrokerPosition }
  | { kind: 'order'; payload: OrderRecord }
  | { kind: 'fill'; payload: unknown }
  | { kind: 'trade_closed'; payload: unknown }
  | { kind: 'risk'; payload: RiskStatus }
  | { kind: 'kill_tripped'; payload: RiskStatus }
  | {
      kind: 'accounts'
      payload: { default_account: string | null; accounts: string[] }
    }

const SELECTED_ACCOUNT_KEY = 'bullish.selected_account'

const RECONNECT_DELAY_MS = 3_000

/**
 * Subscribes to /broker/stream (SSE) for live broker state. The initial
 * `event: snapshot` message seeds account/positions/orders/risk; later
 * deltas patch into that state by symbol or order id. The stream
 * auto-reconnects on close.
 *
 * `liveAcknowledged` is a session-only flag the order ticket flips on
 * after the first live confirmation modal.
 */
export function useBrokerData(): BrokerData {
  const [status, setStatus] = useState<BrokerStatus | null>(null)
  const [account, setAccount] = useState<AccountSnapshot | null>(null)
  const [positions, setPositions] = useState<BrokerPosition[]>([])
  const [orders, setOrders] = useState<OrderRecord[]>([])
  const [risk, setRisk] = useState<RiskStatus | null>(null)
  const [accounts, setAccounts] = useState<string[]>([])
  const [selectedAccount, setSelectedAccountState] = useState<string | null>(
    () => window.localStorage.getItem(SELECTED_ACCOUNT_KEY),
  )
  const [error, setError] = useState<string | null>(null)
  const [liveAcknowledged, setLiveAcknowledged] = useState(false)

  const setSelectedAccount = useCallback((acct: string | null) => {
    setSelectedAccountState(acct)
    if (acct) window.localStorage.setItem(SELECTED_ACCOUNT_KEY, acct)
    else window.localStorage.removeItem(SELECTED_ACCOUNT_KEY)
  }, [])

  const sourceRef = useRef<EventSource | null>(null)
  const reconnectRef = useRef<number | null>(null)
  const cancelledRef = useRef(false)

  const refresh = useCallback(async () => {
    // SSE pushes deltas, but expose a manual refresh anyway so the UI
    // can re-poll risk-status (idempotent fallback).
    try {
      const r = await fetchRiskStatus()
      setRisk(r)
    } catch (e) {
      setError(String(e))
    }
  }, [])

  const resetKillSwitch = useCallback(async () => {
    try {
      const r = await resetRisk()
      setRisk(r)
    } catch (e) {
      setError(String(e))
      throw e
    }
  }, [])

  // 1. One-shot status probe on mount (decides whether to open SSE).
  useEffect(() => {
    let cancelled = false
    fetchBrokerStatus()
      .then((s) => {
        if (cancelled) return
        setStatus(s)
        if (s.accounts && s.accounts.length > 0) {
          setAccounts(s.accounts)
        }
      })
      .catch((e) => {
        if (cancelled) return
        setError(String(e))
        setStatus({ connected: false, paper: null, hint: null })
      })
    return () => {
      cancelled = true
    }
  }, [])

  // 2. SSE subscription — only if connected.
  useEffect(() => {
    if (!status?.connected) return
    cancelledRef.current = false

    const open = () => {
      if (cancelledRef.current) return
      const es = new EventSource(`${BASE_URL}/broker/stream`)
      sourceRef.current = es

      es.addEventListener('snapshot', (ev) => {
        try {
          const snap = JSON.parse((ev as MessageEvent).data) as BrokerSnapshot
          setAccount(snap.account)
          setPositions(snap.positions)
          setOrders(snap.orders)
          setRisk(snap.risk)
          if (snap.accounts && snap.accounts.length > 0) {
            setAccounts(snap.accounts)
          }
          setError(null)
        } catch (e) {
          setError(`snapshot parse: ${String(e)}`)
        }
      })

      es.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as StreamEvent
          applyStreamEvent(msg, {
            setAccount,
            setPositions,
            setOrders,
            setRisk,
            setAccounts,
          })
        } catch (e) {
          // Heartbeats arrive as comment lines and don't fire onmessage,
          // so any parse failure here is real and worth surfacing.
          setError(`stream parse: ${String(e)}`)
        }
      }

      es.onerror = () => {
        es.close()
        sourceRef.current = null
        if (cancelledRef.current) return
        // Retry shortly. EventSource fires onerror on network blips and
        // when the server drops; in both cases reopening is the right
        // move.
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
  }, [status?.connected])

  const acknowledgeLive = useCallback(() => setLiveAcknowledged(true), [])

  // Default the selection to the broker's reported default account once
  // we know it, but only if the user hasn't explicitly picked something
  // (and the persisted value isn't in the managed list — could happen
  // after switching IB env to a different account set).
  useEffect(() => {
    if (accounts.length === 0) return
    if (selectedAccount && accounts.includes(selectedAccount)) return
    const fallback = status?.default_account ?? status?.account ?? accounts[0] ?? null
    setSelectedAccount(fallback)
  }, [accounts, selectedAccount, status?.default_account, status?.account, setSelectedAccount])

  return {
    status,
    account,
    positions,
    orders,
    risk,
    accounts,
    selectedAccount,
    setSelectedAccount,
    error,
    refresh,
    resetKillSwitch,
    liveAcknowledged,
    acknowledgeLive,
  }
}

function applyStreamEvent(
  ev: StreamEvent,
  setters: {
    setAccount: (a: AccountSnapshot | null) => void
    setPositions: (fn: (prev: BrokerPosition[]) => BrokerPosition[]) => void
    setOrders: (fn: (prev: OrderRecord[]) => OrderRecord[]) => void
    setRisk: (r: RiskStatus | null) => void
    setAccounts: (a: string[]) => void
  },
): void {
  switch (ev.kind) {
    case 'account':
      setters.setAccount(ev.payload)
      return
    case 'position': {
      const next = ev.payload
      setters.setPositions((prev) => {
        const others = prev.filter((p) => p.symbol !== next.symbol)
        // Server sends qty=0 to indicate flat — drop it from the list.
        if (!next.qty) return others
        return [...others, next]
      })
      return
    }
    case 'order': {
      const next = ev.payload
      setters.setOrders((prev) => {
        const idx = prev.findIndex((o) => o.id === next.id)
        if (idx === -1) return [next, ...prev].slice(0, 50)
        const copy = prev.slice()
        copy[idx] = next
        return copy
      })
      return
    }
    case 'risk':
    case 'kill_tripped':
      setters.setRisk(ev.payload)
      return
    case 'accounts':
      setters.setAccounts(ev.payload.accounts ?? [])
      return
    case 'fill':
    case 'trade_closed':
      // No-op for the broker hook — the Journal panel polls separately
      // when it's open. (Could trigger a refetch event here if needed.)
      return
  }
}
