import type {
  AccountSnapshot,
  AccountSummaryRow,
  BacktestParams,
  BacktestReport,
  BrokerPosition,
  BrokerStatus,
  CloseAllResult,
  ConnectionInfo,
  JournalStats,
  JournalTrade,
  OrderRecord,
  RiskStatus,
  ScanResponse,
  SectorRotationResponse,
  SubmitOrderRequest,
  SubmitOrderResponse,
} from './types'

export const BASE_URL = import.meta.env.VITE_API_BASE ?? 'http://localhost:8787'

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`${path} failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<T>
}

export interface ScanParams {
  interval?: string
  lookback_days?: number
  tickers?: string[]
}

export async function runScan(provider: string, params: ScanParams = {}): Promise<ScanResponse> {
  return post<ScanResponse>('/scan', {
    provider,
    ...(params.interval ? { interval: params.interval } : {}),
    ...(params.lookback_days ? { lookback_days: params.lookback_days } : {}),
    ...(params.tickers ? { tickers: params.tickers } : {}),
  })
}

export async function runBacktest(
  provider: string,
  params: Partial<BacktestParams> = {},
): Promise<BacktestReport> {
  return post<BacktestReport>('/backtest', {
    provider,
    holding_bars: params.holding_bars ?? 3,
    target_pct: params.target_pct ?? 2.0,
    ...(params.lookback_days ? { lookback_days: params.lookback_days } : {}),
    ...(params.tickers ? { tickers: params.tickers } : {}),
  })
}

export async function fetchHealth(): Promise<{ status: string }> {
  const res = await fetch(`${BASE_URL}/health`)
  if (!res.ok) throw new Error(`health failed: ${res.status}`)
  return res.json() as Promise<{ status: string }>
}

export interface PresetSummary {
  name: string
  interval: string
  lookback_days: number
  n_tickers: number
}

export async function listPresets(): Promise<PresetSummary[]> {
  const res = await fetch(`${BASE_URL}/presets`)
  if (!res.ok) throw new Error(`presets failed: ${res.status}`)
  const data = (await res.json()) as { presets: PresetSummary[] }
  return data.presets
}

export async function runSectorRotation(
  provider: string,
  params: { interval?: string; lookback_days?: number; top_n?: number } = {},
): Promise<SectorRotationResponse> {
  return post<SectorRotationResponse>('/scan/sector-rotation', {
    provider,
    ...(params.interval ? { interval: params.interval } : {}),
    ...(params.lookback_days ? { lookback_days: params.lookback_days } : {}),
    ...(params.top_n ? { top_n: params.top_n } : {}),
  })
}

export async function fetchWatchlist(): Promise<string[]> {
  const res = await fetch(`${BASE_URL}/watchlist`)
  if (!res.ok) throw new Error(`watchlist failed: ${res.status}`)
  const data = (await res.json()) as { tickers: string[] }
  return data.tickers
}

export async function saveWatchlist(tickers: string[]): Promise<string[]> {
  const res = await fetch(`${BASE_URL}/watchlist`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ tickers }),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`watchlist save failed: ${res.status} ${text}`)
  }
  const data = (await res.json()) as { tickers: string[] }
  return data.tickers
}

export async function fetchBrokerStatus(): Promise<BrokerStatus> {
  const res = await fetch(`${BASE_URL}/broker/status`)
  if (!res.ok) throw new Error(`broker status failed: ${res.status}`)
  return res.json() as Promise<BrokerStatus>
}

export async function fetchAccount(): Promise<AccountSnapshot> {
  const res = await fetch(`${BASE_URL}/broker/account`)
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`account failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<AccountSnapshot>
}

export async function fetchPositions(): Promise<BrokerPosition[]> {
  const res = await fetch(`${BASE_URL}/broker/positions`)
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`positions failed: ${res.status} ${text}`)
  }
  const data = (await res.json()) as { positions: BrokerPosition[] }
  return data.positions
}

export async function closeAllPositions(): Promise<CloseAllResult> {
  const res = await fetch(`${BASE_URL}/broker/close-all`, { method: 'POST' })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`close-all failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<CloseAllResult>
}

export async function fetchConnections(): Promise<ConnectionInfo[]> {
  const res = await fetch(`${BASE_URL}/broker/connections`)
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`connections fetch failed: ${res.status} ${text}`)
  }
  const data = (await res.json()) as { connections: ConnectionInfo[] }
  return data.connections
}

export async function connectBroker(label: string): Promise<ConnectionInfo> {
  const res = await fetch(
    `${BASE_URL}/broker/connections/${encodeURIComponent(label)}/connect`,
    { method: 'POST' },
  )
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`connect ${label} failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<ConnectionInfo>
}

export async function disconnectBroker(label: string): Promise<ConnectionInfo> {
  const res = await fetch(
    `${BASE_URL}/broker/connections/${encodeURIComponent(label)}/disconnect`,
    { method: 'POST' },
  )
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`disconnect ${label} failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<ConnectionInfo>
}

export interface ConnectionUpsert {
  label: string
  host: string
  port: number
  client_id: number
  paper: boolean
  auto_connect: boolean
  default_account?: string | null
}

export async function createConnection(req: ConnectionUpsert): Promise<ConnectionInfo> {
  const res = await fetch(`${BASE_URL}/broker/connections`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(req),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`create connection failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<ConnectionInfo>
}

export async function updateConnection(
  oldLabel: string,
  req: ConnectionUpsert,
): Promise<ConnectionInfo> {
  const res = await fetch(
    `${BASE_URL}/broker/connections/${encodeURIComponent(oldLabel)}`,
    {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(req),
    },
  )
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`update connection failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<ConnectionInfo>
}

export async function deleteConnection(label: string): Promise<void> {
  const res = await fetch(
    `${BASE_URL}/broker/connections/${encodeURIComponent(label)}`,
    { method: 'DELETE' },
  )
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`delete connection failed: ${res.status} ${text}`)
  }
}

export async function fetchAccountAliases(): Promise<Record<string, string>> {
  const res = await fetch(`${BASE_URL}/broker/account-aliases`)
  if (!res.ok) throw new Error(`fetch aliases failed: ${res.status}`)
  return res.json() as Promise<Record<string, string>>
}

export async function saveAccountAliases(
  aliases: Record<string, string>,
): Promise<Record<string, string>> {
  const res = await fetch(`${BASE_URL}/broker/account-aliases`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ aliases }),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`save aliases failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<Record<string, string>>
}

export async function fetchAccountsSummary(
  connection?: string,
): Promise<{ accounts: AccountSummaryRow[]; errors: { connection: string; error: string }[] }> {
  const url = connection
    ? `${BASE_URL}/broker/accounts-summary?connection=${encodeURIComponent(connection)}`
    : `${BASE_URL}/broker/accounts-summary`
  const res = await fetch(url)
  if (!res.ok) throw new Error(`accounts-summary failed: ${res.status}`)
  return res.json() as Promise<{
    accounts: AccountSummaryRow[]
    errors: { connection: string; error: string }[]
  }>
}

export async function submitOrder(req: SubmitOrderRequest): Promise<SubmitOrderResponse> {
  const res = await fetch(`${BASE_URL}/broker/orders`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(req),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`submit order failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<SubmitOrderResponse>
}

export async function closePosition(
  symbol: string,
  options: {
    percentage?: number
    qty?: number
    account?: string | null
    connection?: string | null
  } = {},
): Promise<OrderRecord> {
  const params = new URLSearchParams()
  if (options.percentage !== undefined) params.set('percentage', String(options.percentage))
  if (options.qty !== undefined) params.set('qty', String(options.qty))
  if (options.account) params.set('account', options.account)
  if (options.connection) params.set('connection', options.connection)
  const qs = params.toString() ? `?${params.toString()}` : ''
  const res = await fetch(
    `${BASE_URL}/broker/positions/${encodeURIComponent(symbol)}${qs}`,
    { method: 'DELETE' },
  )
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`close position failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<OrderRecord>
}

export async function cancelOrdersForSymbol(
  symbol: string,
  connection?: string | null,
): Promise<{ symbol: string; canceled: number; results?: unknown[] }> {
  const params = new URLSearchParams({ symbol })
  if (connection) params.set('connection', connection)
  const res = await fetch(
    `${BASE_URL}/broker/orders?${params.toString()}`,
    { method: 'DELETE' },
  )
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`cancel orders failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<{ symbol: string; canceled: number; results?: unknown[] }>
}

export async function fetchOrders(limit = 30): Promise<OrderRecord[]> {
  const res = await fetch(`${BASE_URL}/broker/orders?limit=${limit}`)
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`orders failed: ${res.status} ${text}`)
  }
  const data = (await res.json()) as { orders: OrderRecord[] }
  return data.orders
}

export async function fetchRiskStatus(): Promise<RiskStatus> {
  const res = await fetch(`${BASE_URL}/broker/risk-status`)
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`risk-status failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<RiskStatus>
}

export async function resetRisk(): Promise<RiskStatus> {
  const res = await fetch(`${BASE_URL}/broker/risk-reset`, { method: 'POST' })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`risk-reset failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<RiskStatus>
}

export async function fetchJournalTrades(limit = 100): Promise<JournalTrade[]> {
  const res = await fetch(`${BASE_URL}/broker/journal/trades?limit=${limit}`)
  if (!res.ok) throw new Error(`journal trades failed: ${res.status}`)
  const data = (await res.json()) as { trades: JournalTrade[] }
  return data.trades
}

export async function fetchJournalStats(): Promise<JournalStats> {
  const res = await fetch(`${BASE_URL}/broker/journal/stats`)
  if (!res.ok) throw new Error(`journal stats failed: ${res.status}`)
  return res.json() as Promise<JournalStats>
}

export async function applyPreset(name: string): Promise<{ interval: string; lookback_days: number }> {
  const res = await fetch(`${BASE_URL}/config/preset/${encodeURIComponent(name)}`, {
    method: 'POST',
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`preset ${name} failed: ${res.status} ${text}`)
  }
  const cfg = (await res.json()) as { interval: string; lookback_days: number }
  return cfg
}
