import {
  NEAR_HIGH_THRESHOLD_PCT,
  type FilterState,
  type TrendFilter,
} from '../useResultFilters'

interface Props {
  state: FilterState
  hasActive: boolean
  toggleTrend: (f: TrendFilter) => void
  toggleGreen: () => void
  toggleNearHigh: () => void
  setMinScore: (n: number) => void
  setRsiMin: (n: number) => void
  setRsiMax: (n: number) => void
  onClear?: () => void
}

const TREND_PILLS: ReadonlyArray<{
  id: TrendFilter
  label: string
  tooltip: string
}> = [
  { id: 'vwap', label: 'VWAP+', tooltip: 'Show only tickers above VWAP' },
  { id: 'ema9', label: 'EMA9+', tooltip: 'Show only tickers above the 9-bar EMA' },
  {
    id: 'stacked',
    label: 'Stacked',
    tooltip: 'Show only tickers with EMA9 > EMA20 > EMA50 (bullish stack)',
  },
]

/** Compact post-scan filter strip. Two rows:
 *
 *   [Filter] [VWAP+] [EMA9+] [Stacked] [Green] [Near20H]    clear
 *   Score ≥ [n]    RSI [min]–[max]
 *
 * All filters are AND-combined. Empty/default values pass through. */
export function ResultFilters({
  state,
  hasActive,
  toggleTrend,
  toggleGreen,
  toggleNearHigh,
  setMinScore,
  setRsiMin,
  setRsiMax,
  onClear,
}: Props) {
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-1">
        <span className="text-[9px] uppercase tracking-wide text-neutral-500">
          Filter
        </span>
        {TREND_PILLS.map((p) => (
          <Pill
            key={p.id}
            label={p.label}
            tooltip={p.tooltip}
            active={state.trend.has(p.id)}
            onClick={() => toggleTrend(p.id)}
          />
        ))}
        <Pill
          label="Green"
          tooltip="Show only tickers up on the day (pct change > 0)"
          active={state.greenDay}
          onClick={toggleGreen}
        />
        <Pill
          label={`Near${NEAR_HIGH_THRESHOLD_PCT}%`}
          tooltip={`Show only tickers within ${NEAR_HIGH_THRESHOLD_PCT}% of the 20-bar high`}
          active={state.nearHigh}
          onClick={toggleNearHigh}
        />
        {hasActive && onClear && (
          <button
            type="button"
            onClick={onClear}
            className="ml-auto text-[10px] text-neutral-500 underline-offset-2 hover:text-neutral-300 hover:underline"
            title="Clear all filters"
          >
            clear
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 text-[10px] text-neutral-500">
        <NumberField
          label="Score ≥"
          value={state.minScore}
          onChange={setMinScore}
          min={0}
          max={100}
          step={1}
          width="w-12"
          tooltip="Show only tickers with score at or above this value (0–100)"
        />
        <span className="flex items-center gap-1">
          <span className="text-[9px] uppercase tracking-wide">RSI</span>
          <NumberField
            value={state.rsiMin}
            onChange={setRsiMin}
            min={0}
            max={100}
            step={1}
            width="w-10"
            tooltip="Minimum RSI"
          />
          <span className="text-neutral-600">–</span>
          <NumberField
            value={state.rsiMax}
            onChange={setRsiMax}
            min={0}
            max={100}
            step={1}
            width="w-10"
            tooltip="Maximum RSI"
          />
        </span>
      </div>
    </div>
  )
}

function Pill({
  label,
  tooltip,
  active,
  onClick,
}: {
  label: string
  tooltip: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={tooltip}
      className={`rounded border px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
        active
          ? 'border-[var(--color-accent)] bg-[var(--color-accent)]/20 text-[var(--color-accent)]'
          : 'border-neutral-700 text-neutral-400 hover:border-neutral-500 hover:text-neutral-200'
      }`}
    >
      {label}
    </button>
  )
}

function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  step,
  width,
  tooltip,
}: {
  label?: string
  value: number
  onChange: (n: number) => void
  min: number
  max: number
  step: number
  width: string
  tooltip?: string
}) {
  return (
    <span className="flex items-center gap-1" title={tooltip}>
      {label && (
        <span className="text-[9px] uppercase tracking-wide">{label}</span>
      )}
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => {
          const n = Number(e.target.value)
          if (Number.isFinite(n)) onChange(n)
        }}
        className={`${width} rounded border border-neutral-700 bg-neutral-900 px-1 py-0.5 text-right text-[10px] text-neutral-200 num`}
      />
    </span>
  )
}
