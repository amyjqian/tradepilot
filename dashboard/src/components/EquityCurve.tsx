import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { EquityPoint } from '../types'

interface Props {
  points: EquityPoint[]
}

export function EquityCurve({ points }: Props) {
  if (points.length === 0) {
    return (
      <div className="flex h-72 items-center justify-center text-sm text-neutral-500">
        No equity data.
      </div>
    )
  }
  const data = points.map((p) => ({ date: p.date.slice(0, 10), equity: p.equity }))
  return (
    <div className="h-72 w-full">
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 10, right: 20, bottom: 10, left: 10 }}>
          <CartesianGrid strokeDasharray="2 2" stroke="currentColor" opacity={0.1} />
          <XAxis dataKey="date" stroke="currentColor" fontSize={11} minTickGap={40} />
          <YAxis stroke="currentColor" fontSize={11} />
          <Tooltip
            contentStyle={{
              backgroundColor: 'rgb(23 23 23)',
              border: '1px solid rgb(64 64 64)',
              color: 'rgb(245 245 245)',
              fontSize: '12px',
            }}
            formatter={(v) => [`${Number(v).toFixed(2)}%`, 'equity']}
          />
          <Line
            dataKey="equity"
            type="monotone"
            stroke="var(--color-accent)"
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
