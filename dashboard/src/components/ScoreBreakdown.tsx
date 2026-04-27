import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

interface Props {
  signals: Record<string, number>
}

export function ScoreBreakdown({ signals }: Props) {
  const data = Object.entries(signals).map(([name, value]) => ({
    name: name.replace(/_/g, ' '),
    value: Math.round(value * 1000) / 10, // 0-100
  }))

  return (
    <div className="h-40 w-full">
      <ResponsiveContainer>
        <BarChart layout="vertical" data={data} margin={{ top: 4, right: 12, bottom: 4, left: 80 }}>
          <CartesianGrid strokeDasharray="2 2" stroke="currentColor" opacity={0.1} />
          <XAxis type="number" domain={[0, 100]} stroke="currentColor" fontSize={11} />
          <YAxis
            type="category"
            dataKey="name"
            stroke="currentColor"
            fontSize={11}
            width={80}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: 'rgb(23 23 23)',
              border: '1px solid rgb(64 64 64)',
              color: 'rgb(245 245 245)',
              fontSize: '12px',
            }}
            formatter={(v) => [`${Number(v).toFixed(1)}`, 'strength']}
          />
          <Bar dataKey="value" fill="var(--color-accent)" radius={[0, 3, 3, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
