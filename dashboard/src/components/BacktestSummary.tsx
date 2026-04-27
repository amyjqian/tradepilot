import type { BacktestReport } from '../types'
import { fmtNumber, fmtPct } from '../format'

interface Props {
  report: BacktestReport
}

export function BacktestSummary({ report }: Props) {
  const metrics: Array<[string, string]> = [
    ['Signals', String(report.n_signals)],
    ['Winners', String(report.n_winners)],
    ['Hit rate', fmtPct(report.hit_rate * 100)],
    ['Avg return', fmtPct(report.avg_return_pct, true)],
    ['Median return', fmtPct(report.median_return_pct, true)],
    ['Avg max run-up', fmtPct(report.avg_max_return_pct, true)],
    ['Avg max drawdown', fmtPct(report.avg_max_drawdown_pct, true)],
    ['Profit factor', fmtNumber(report.profit_factor)],
    ['Expectancy', fmtPct(report.expectancy_pct, true)],
  ]
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
      {metrics.map(([k, v]) => (
        <div
          key={k}
          className="rounded-lg border border-neutral-200 p-3 dark:border-neutral-800"
        >
          <div className="text-xs uppercase tracking-wide text-neutral-500">{k}</div>
          <div className="num mt-1 text-base font-medium">{v}</div>
        </div>
      ))}
    </div>
  )
}
