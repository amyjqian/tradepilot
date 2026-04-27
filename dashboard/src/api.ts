import type { BacktestParams, BacktestReport, ScanResponse } from './types'

const BASE_URL = import.meta.env.VITE_API_BASE ?? 'http://localhost:8787'

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
