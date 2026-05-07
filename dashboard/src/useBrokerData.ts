import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  BASE_URL,
  fetchBrokerStatus,
  fetchConnections,
  fetchRiskStatus,
  resetRisk,
} from './api'
import type {
  AccountSnapshot,
  BrokerPosition,
  BrokerSnapshot,
  BrokerStatus,
  ConnectionInfo,
  OrderRecord,
  OrderTarget,
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
  /** Configured TWS connections + their live accounts. */
  connections: ConnectionInfo[]
  /** One row per (connection, account) the user has checked as an
   * order destination. Empty list = use the broker's default. */
  selectedTargets: OrderTarget[]
  setSelectedTargets: (targets: OrderTarget[]) => void
  refreshConnections: () => Promise<void>
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

const SELECTED_ACCOUNT_KEY = 'tradepilot.selected_account'
const SELECTED_TARGETS_KEY = 'tradepilot.selected_targets'

const RECONNECT_DELAY_MS = 3_000

function loadStoredTargets(): OrderTarget[] {
  try {
    const raw = window.localStorage.getItem(SELECTED_TARGETS_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((x): x is OrderTarget =>
        x && typeof x === 'object' && (typeof x.connection === 'string' || x.connection === undefined),
      )
  } catch {
    return []
  }
}

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
  const [legacyAccounts, setLegacyAccounts] = useState<string[]>([])
  const [selectedAccount, setSelectedAccountState] = useState<string | null>(
    () => window.localStorage.getItem(SELECTED_ACCOUNT_KEY),
  )
  const [error, setError] = useState<string | null>(null)
  const [liveAcknowledged, setLiveAcknowledged] = useState(false)
  const [connections, setConnections] = useState<ConnectionInfo[]>([])
  const [selectedTargets, setSelectedTargetsState] = useState<OrderTarget[]>(
    () => loadStoredTargets(),
  )

  // Flat list of all accounts visible across all connections — drop-in
  // replacement for the old `accounts: string[]` so the existing
  // single-select UI still has something to render.
  const accounts = useMemo<string[]>(() => {
    if (connections.length === 0) return legacyAccounts
    const all: string[] = []
    for (const c of connections) for (const a of c.accounts) all.push(a)
    return all.length > 0 ? all : legacyAccounts
  }, [connections, legacyAccounts])

  const setSelectedAccount = useCallback((acct: string | null) => {
    setSelectedAccountState(acct)
    if (acct) window.localStorage.setItem(SELECTED_ACCOUNT_KEY, acct)
    else window.localStorage.removeItem(SELECTED_ACCOUNT_KEY)
  }, [])

  const setSelectedTargets = useCallback((targets: OrderTarget[]) => {
    setSelectedTargetsState(targets)
    window.localStorage.setItem(SELECTED_TARGETS_KEY, JSON.stringify(targets))
  }, [])

  const refreshConnections = useCallback(async () => {
    try {
      const list = await fetchConnections()
      setConnections(list)
    } catch (e) {
      setError(String(e))
    }
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

  // 1. One-shot status probe + connections list on mount.
  useEffect(() => {
    let cancelled = false
    fetchBrokerStatus()
      .then((s) => {
        if (cancelled) return
        setStatus(s)
        if (s.accounts && s.accounts.length > 0) {
          setLegacyAccounts(s.accounts)
        }
      })
      .catch((e) => {
        if (cancelled) return
        setError(String(e))
        setStatus({ connected: false, paper: null, hint: null })
      })
    // Skip the connections list when broker is disabled — the endpoint
    // would 200 with an empty list anyway, but no need to call it.
    fetchConnections()
      .then((list) => {
        if (cancelled) return
        setConnections(list)
      })
      .catch(() => {
        // Older API without connections endpoint — just leave empty.
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Re-poll connections periodically so newly-connected TWS instances
  // show up without a manual refresh. Skipped when broker is disabled
  // — the disabled flag won't change during a session, so polling is
  // pure noise (and a wasted HTTP request every 8s).
  useEffect(() => {
    if (status?.disabled) return
    const id = window.setInterval(() => void refreshConnections(), 8_000)
    return () => window.clearInterval(id)
  }, [refreshConnections, status?.disabled])

  // 2. SSE subscription — only if connected (and broker isn't disabled).
  useEffect(() => {
    if (status?.disabled) return
    if (!status?.connected) return
    cancelledRef.current = false

    const open = () => {
      if (cancelledRef.current) return
      const es = new EventSource(`${BASE_URL}/broker/stream`)
      sourceRef.current = es

      es.addEventListener('snapshot', (ev) => {
        try {
          const snap = JSON.parse((ev as MessageEvent).data) as Partial<BrokerSnapshot>
          // Be defensive — multi-connection snapshots may omit some
          // top-level fields, and a malformed payload shouldn't blank the UI.
          if (snap.account !== undefined) setAccount(snap.account)
          if (Array.isArray(snap.positions)) setPositions(snap.positions)
          if (Array.isArray(snap.orders)) setOrders(snap.orders)
          if (snap.risk !== undefined) setRisk(snap.risk)
          if (Array.isArray(snap.accounts) && snap.accounts.length > 0) {
            setLegacyAccounts(snap.accounts)
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
            setAccounts: setLegacyAccounts,
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

  // Default the single-account selection (legacy) to the broker's default.
  useEffect(() => {
    if (accounts.length === 0) return
    if (selectedAccount && accounts.includes(selectedAccount)) return
    const fallback = status?.default_account ?? status?.account ?? accounts[0] ?? null
    setSelectedAccount(fallback)
  }, [accounts, selectedAccount, status?.default_account, status?.account, setSelectedAccount])

  // Default `selectedTargets` once we have connections — pick the first
  // connection's first account so order entry works out of the box.
  // Skip if the user has already picked something (validated against the
  // current connection list to drop stale targets after a config edit).
  useEffect(() => {
    if (connections.length === 0) return
    const validKey = (t: OrderTarget) =>
      connections.some(
        (c) =>
          c.label === t.connection &&
          (t.account === undefined || c.accounts.includes(t.account)),
      )
    const valid = selectedTargets.filter(validKey)
    if (valid.length > 0) {
      if (valid.length !== selectedTargets.length) setSelectedTargets(valid)
      return
    }
    const firstConn = connections[0]
    const firstAcct = firstConn.default_account ?? firstConn.accounts[0]
    if (firstAcct) {
      setSelectedTargets([{ connection: firstConn.label, account: firstAcct }])
    }
  }, [connections, selectedTargets, setSelectedTargets])

  return {
    status,
    account,
    positions,
    orders,
    risk,
    accounts,
    selectedAccount,
    setSelectedAccount,
    connections,
    selectedTargets,
    setSelectedTargets,
    refreshConnections,
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
        // Multi-account / multi-connection: a single symbol can appear
        // in multiple rows. Dedupe by `(connection, account, symbol)` so
        // an update to one row never clobbers the others.
        const key = (p: BrokerPosition) =>
          `${p.connection ?? ''}::${p.account ?? ''}::${p.symbol}`
        const nextKey = key(next)
        const others = prev.filter((p) => key(p) !== nextKey)
        if (!next.qty) return others // server sends qty=0 to mean "flat"
        return [...others, next]
      })
      return
    }
    case 'order': {
      const next = ev.payload
      setters.setOrders((prev) => {
        // Order ids are unique only per-IB-client, so two connections
        // can collide. Key by `(connection, id)` to keep them apart.
        const key = (o: OrderRecord) => `${o.connection ?? ''}::${o.id}`
        const nextKey = key(next)
        const idx = prev.findIndex((o) => key(o) === nextKey)
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
