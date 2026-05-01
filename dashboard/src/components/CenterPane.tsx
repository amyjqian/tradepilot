import type { ScanResult } from '../types'
import { fmtCurrency, fmtMultiplier, fmtNumber, fmtPct } from '../format'
import { ScoreBreakdown } from './ScoreBreakdown'
import { TradingViewChart } from './TradingViewChart'

interface Props {
  selected: ScanResult | null
}

export function CenterPane({ selected }: Props) {
  if (!selected) {
    return (
      <section className="flex h-full items-center justify-center bg-neutral-950 text-sm text-neutral-500">
        <div className="space-y-1 text-center">
          <p>No ticker selected.</p>
          <p className="text-xs">
            Run a scan in the left rail and click a row to load chart + signals.
          </p>
        </div>
      </section>
    )
  }

  const r = selected
  return (
    <section className="flex h-full flex-col gap-2 bg-neutral-950 p-2">
      <header className="flex flex-wrap items-center gap-3 border-b border-neutral-800 pb-2">
        <h2 className="text-base font-semibold">{r.ticker}</h2>
        <span className="num text-sm">{fmtCurrency(r.price)}</span>
        <span
          className={`num text-sm ${
            r.pct_change >= 0
              ? 'text-[var(--color-accent-dim)]'
              : 'text-[var(--color-danger)]'
          }`}
        >
          {fmtPct(r.pct_change, true)}
        </span>
        <span className="ml-auto flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-wide text-neutral-500">Score</span>
          <ScoreBadge score={r.score} />
          <a
            className="text-[10px] text-[var(--color-accent-dim)] hover:underline"
            href={`https://www.tradingview.com/chart/?symbol=${r.ticker}`}
            target="_blank"
            rel="noreferrer"
          >
            Open ↗
          </a>
        </span>
      </header>

      <div className="min-h-0 flex-[1.6]">
        <TradingViewChart ticker={r.ticker} height="fill" />
      </div>

      <div className="grid min-h-0 flex-1 gap-2 lg:grid-cols-3">
        <Card title="Quick stats">
          <ul className="space-y-1 text-xs">
            <Stat label="RSI" value={fmtNumber(r.rsi)} />
            <Stat label="Rel Vol" value={fmtMultiplier(r.rel_volume)} />
            <Stat
              label="Distance from 20d high"
              value={fmtPct(r.dist_from_20d_high_pct, true)}
            />
            <Stat
              label="Above VWAP"
              value={r.above_vwap ? 'Yes' : 'No'}
              good={r.above_vwap}
            />
            <Stat
              label="Above EMA9"
              value={r.above_ema9 ? 'Yes' : 'No'}
              good={r.above_ema9}
            />
            <Stat
              label="EMA stacked"
              value={r.ema_stacked ? 'Yes' : 'No'}
              good={r.ema_stacked}
            />
          </ul>
        </Card>

        <Card title="Signal strengths">
          <ScoreBreakdown signals={r.signals} />
        </Card>

        <Card title="Reasons">
          {r.reasons.length === 0 ? (
            <p className="text-xs text-neutral-500">No qualitative factors</p>
          ) : (
            <ul className="list-disc space-y-1 pl-4 text-xs">
              {r.reasons.map((reason, i) => (
                <li key={i}>{reason}</li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </section>
  )
}

function ScoreBadge({ score }: { score: number }) {
  const color =
    score >= 50
      ? 'bg-[var(--color-accent)]'
      : score >= 30
        ? 'bg-[var(--color-warn)]'
        : 'bg-[var(--color-danger)]'
  return (
    <span className="flex items-center gap-1.5">
      <span className="h-1.5 w-16 overflow-hidden rounded bg-neutral-800">
        <span
          className={`block h-full ${color}`}
          style={{ width: `${Math.min(100, Math.max(0, score))}%` }}
        />
      </span>
      <span className="num text-xs">{fmtNumber(score)}</span>
    </span>
  )
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-0 flex-col rounded border border-neutral-800 bg-neutral-900/40 p-2">
      <div className="mb-1 text-[10px] uppercase tracking-wide text-neutral-500">{title}</div>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </div>
  )
}

function Stat({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return (
    <li className="flex items-center justify-between">
      <span className="text-neutral-500">{label}</span>
      <span
        className={`num ${
          good === true
            ? 'text-[var(--color-accent-dim)]'
            : good === false
              ? 'text-neutral-400'
              : ''
        }`}
      >
        {value}
      </span>
    </li>
  )
}
