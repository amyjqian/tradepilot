import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ScanTable } from './components/ScanTable'
import { BacktestSummary } from './components/BacktestSummary'
import type { ScanResult, BacktestReport } from './types'

const mockResults: ScanResult[] = [
  {
    ticker: 'NVDA',
    score: 78.2,
    price: 520.13,
    pct_change: 2.1,
    rel_volume: 2.3,
    rsi: 62,
    above_vwap: true,
    above_ema9: true,
    ema_stacked: true,
    dist_from_20d_high_pct: 0.4,
    signals: { relative_volume: 0.7, momentum: 0.4, trend_alignment: 1.0, rsi_position: 0.9, breakout_proximity: 0.9 },
    reasons: ['Relative volume 2.30x average', 'Up 2.10% on the day'],
  },
]

const mockReport: BacktestReport = {
  n_signals: 12,
  n_winners: 7,
  hit_rate: 0.5833,
  avg_return_pct: 1.4,
  median_return_pct: 1.2,
  avg_max_return_pct: 2.8,
  avg_max_drawdown_pct: -1.1,
  profit_factor: 2.4,
  expectancy_pct: 1.4,
  trades: [],
  equity_curve: [],
}

describe('ScanTable', () => {
  it('renders ticker row', () => {
    render(<ScanTable results={mockResults} />)
    expect(screen.getByText('NVDA')).toBeInTheDocument()
  })

  it('shows empty state', () => {
    render(<ScanTable results={[]} />)
    expect(screen.getByText(/Run Scan/i)).toBeInTheDocument()
  })
})

describe('BacktestSummary', () => {
  it('renders all nine metric cards', () => {
    render(<BacktestSummary report={mockReport} />)
    expect(screen.getByText(/Signals/)).toBeInTheDocument()
    expect(screen.getByText(/Profit factor/i)).toBeInTheDocument()
    expect(screen.getByText(/Hit rate/i)).toBeInTheDocument()
  })
})
