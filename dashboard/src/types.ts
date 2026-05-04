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

export interface SectorRank {
  etf: string
  name: string
  score: number
  pct_change_1: number
  pct_change_5: number
  excess_return_5_vs_spy: number
  above_ema20: boolean
}

export interface SectorRotationResponse {
  ran_at: string
  provider: string
  interval: string
  lookback_days: number
  ranked: SectorRank[]
  /** Strongest N sectors (rank order), N controlled by request `top_n`. */
  top_etfs: string[]
  top_names: string[]
  /** Pooled top-N constituents (deduped, rank order). */
  top_constituents: string[]
  /** Per-sector constituent lists — keys are sector ETFs, values are
   * the tickers in that sector. Used to filter `results` to one sector
   * when the user clicks a sector row. */
  top_constituents_by_sector: Record<string, string[]>
  results: ScanResult[]
}

export interface BrokerStatus {
  connected: boolean
  paper: boolean | null
  /** Legacy alias for `default_account`. */
  account?: string | null
  default_account?: string | null
  /** All accounts visible from this connection. Empty until the broker
   * has actually connected (lazy on first /broker/account call). */
  accounts?: string[]
  hint: string | null
}

export interface AccountSnapshot {
  equity: number
  last_equity: number
  cash: number
  buying_power: number
  portfolio_value: number
  pnl_today_abs: number
  pnl_today_pct: number
  paper: boolean
  status: string
}

export interface BrokerPosition {
  symbol: string
  qty: number
  avg_entry_price: number
  current_price: number
  market_value: number
  cost_basis: number
  unrealized_pl_abs: number
  unrealized_pl_pct: number
  side: string
  /** Source TWS connection label. Tagged by the API when aggregating
   * across multiple connections; absent in single-connection legacy
   * payloads. */
  connection?: string
  /** IB account holding this position. Set when known. */
  account?: string
}

/** Connection definition + live status (`/broker/connections`). */
export interface ConnectionInfo {
  label: string
  host: string
  port: number
  client_id: number
  paper: boolean
  auto_connect: boolean
  default_account: string | null
  /** Managed accounts visible from this connection. Empty before the
   * first successful TWS handshake. */
  accounts: string[]
  connected: boolean
}

/** Per-account financial summary (`/broker/accounts-summary`). One row
 * per (connection, account). Numbers are floats in the account's
 * currency (assumed USD). */
export interface AccountSummaryRow {
  connection: string
  account: string
  paper: boolean
  alias: string
  net_liquidation: number
  total_cash: number
  excess_liquidity: number
  realized_pnl: number
  unrealized_pnl: number
  daily_pnl: number
}

/** One destination for a fan-out order. Either field may be omitted —
 * the server falls back to default connection / default account. */
export interface OrderTarget {
  connection?: string
  account?: string
}

export interface CloseAllResult {
  submitted: number
  ok: number
  failed: number
  details: Array<{ symbol: string; status: number; ok: boolean }>
}

export interface OrderRecord {
  id: string
  symbol: string
  side: string
  qty: number
  filled_qty: number
  type: string
  time_in_force: string
  limit_price: number | null
  status: string
  submitted_at: string | null
  filled_at: string | null
  filled_avg_price: number | null
  /** Connection label this order was placed through. Tagged by the API. */
  connection?: string
  /** IB account this order routed to. Set when known. */
  account?: string
}

export interface SubmitOrderRequest {
  symbol: string
  qty: number
  side: 'buy' | 'sell'
  type?: 'market' | 'limit' | 'stop' | 'pegprim' | 'midprice'
  time_in_force?: 'day' | 'gtc'
  limit_price?: number
  /** Trigger price for plain stop orders (type === 'stop'). */
  stop_price?: number
  /** Planned stop captured for journal R-multiple math. */
  planned_stop?: number
  /** Scanner score at the moment of submission. */
  score_at_entry?: number
  /** Pegged-to-Primary (REL) auxPrice — tick offset above bid (BUY) /
   * below ask (SELL). Only used when `type === 'pegprim'`. */
  peg_offset?: number
  /** Pegged-to-Primary (REL) lmtPrice — hard ceiling (BUY) / floor
   * (SELL). Only used when `type === 'pegprim'`. */
  cap_price?: number
  /** Multi-target fan-out. One IB placeOrder per entry — each gets its
   * own row in the response's `orders[]`. Preferred over the legacy
   * `account` / `connection` fields. */
  targets?: OrderTarget[]
  /** Legacy single-target shorthand. Only used when `targets` is absent. */
  connection?: string
  account?: string
}

/** Response from `POST /broker/orders`. The order is placed once per
 * target, so even single-target callers get a list back. */
export interface SubmitOrderResponse {
  orders: OrderRecord[]
  failures: Array<{ connection?: string; account?: string; error: string }>
}

export interface RiskStatus {
  date_et: string | null
  start_equity: number | null
  current_equity: number | null
  drawdown_pct: number | null
  limit_pct: number
  kill_active: boolean
  kill_reason: string | null
  kill_tripped_at: string | null
  enabled: boolean
}

export interface JournalTrade {
  id: number
  symbol: string
  side: 'long' | 'short'
  opened_at: string
  closed_at: string
  qty: number
  entry_avg: number
  exit_avg: number
  planned_stop: number | null
  score_at_entry: number | null
  r_multiple: number | null
  pnl_abs: number
  pnl_pct: number
  holding_sec: number
  win: 0 | 1
}

export interface JournalStats {
  n_trades: number
  wins: number
  losses: number
  win_rate_pct: number
  avg_r: number
  avg_pnl_pct: number
  total_pnl_abs: number
  avg_hold_sec: number
}

/** Initial event payload from /broker/stream — full snapshot. */
export interface BrokerSnapshot {
  account: AccountSnapshot | null
  positions: BrokerPosition[]
  orders: OrderRecord[]
  risk: RiskStatus
  accounts?: string[]
  default_account?: string | null
}
