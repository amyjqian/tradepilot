export interface ScanResult {
  ticker: string
  score: number
  price: number
  pct_change: number
  rel_volume: number
  rsi: number
  above_vwap: boolean
  above_ema9: boolean
  ema_stacked: boolean
  dist_from_20d_high_pct: number
  signals: Record<string, number>
  reasons: string[]
}

export interface ScanResponse {
  ran_at: string
  provider: string
  interval: string
  lookback_days: number
  n_candidates_scanned: number
  n_results: number
  results: ScanResult[]
}

export interface TradeOutcome {
  ticker: string
  entry_date: string
  entry_price: number
  score: number
  max_return_pct: number
  min_return_pct: number
  close_return_pct: number
  hit_target: boolean
}

export interface EquityPoint {
  date: string
  equity: number
  n_trades: number
}

export interface BacktestReport {
  ran_at?: string
  provider?: string
  n_signals: number
  n_winners: number
  hit_rate: number
  avg_return_pct: number
  median_return_pct: number
  avg_max_return_pct: number
  avg_max_drawdown_pct: number
  profit_factor: number
  expectancy_pct: number
  trades: TradeOutcome[]
  equity_curve: EquityPoint[]
  holding_bars?: number
  target_pct?: number
}

export interface BacktestParams {
  provider: string
  holding_bars?: number
  target_pct?: number
  lookback_days?: number
  tickers?: string[]
}
