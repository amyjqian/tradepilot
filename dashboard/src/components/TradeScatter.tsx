import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
  Cell,
} from 'recharts'
import type { TradeOutcome } from '../types'

interface Props {
  trades: TradeOutcome[]
}

export function TradeScatter({ trades }: Props) {
  if (trades.length === 0) {
    return (
      <div className="flex h-72 items-center justify-center text-sm text-neutral-500">
        No trades to plot.
      </div>
    )
  }
  const data = trades.map((t) => ({
    score: t.score,
    return_pct: t.close_return_pct,
    ticker: t.ticker,
    entry_date: t.entry_date.slice(0, 10),
    winner: t.hit_target,
  }))
  return (
    <div className="h-72 w-full">
      <ResponsiveContainer>
        <ScatterChart margin={{ top: 10, right: 20, bottom: 10, left: 10 }}>
          <CartesianGrid strokeDasharray="2 2" stroke="currentColor" opacity={0.1} />
          <XAxis
            type="number"
            dataKey="score"
            name="Score"
            domain={[0, 100]}
            stroke="currentColor"
            fontSize={11}
          />
          <YAxis
            type="number"
            dataKey="return_pct"
            name="Close return %"
            stroke="currentColor"
            fontSize={11}
          />
          <ZAxis range={[30, 30]} />
          <Tooltip
            cursor={{ strokeDasharray: '3 3' }}
            contentStyle={{
              backgroundColor: 'rgb(23 23 23)',
              border: '1px solid rgb(64 64 64)',
              color: 'rgb(245 245 245)',
              fontSize: '12px',
            }}
            formatter={(value, name) => {
              const v = Number(value)
              if (name === 'Score') return [v.toFixed(1), name]
              if (name === 'Close return %') return [`${v.toFixed(2)}%`, name]
              return [String(value), String(name)]
            }}
            labelFormatter={(_, payload) => {
              const p = payload?.[0]?.payload
              if (!p) return ''
              return `${p.ticker} · ${p.entry_date}`
            }}
          />
          <Scatter data={data} fillOpacity={0.7}>
            {data.map((d, i) => (
              <Cell
                key={i}
                fill={d.winner ? 'var(--color-accent)' : 'var(--color-danger)'}
              />
            ))}
          </Scatter>
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  )
}
