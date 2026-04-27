const currencyFmt = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

const decimalFmt = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

const percentFmt = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function fmtCurrency(value: number): string {
  // -$5.20 instead of $-5.20.
  if (value < 0) return `-${currencyFmt.format(Math.abs(value))}`
  return currencyFmt.format(value)
}

export function fmtPct(value: number, signed = false): string {
  const s = percentFmt.format(Math.abs(value))
  if (signed) return `${value >= 0 ? '+' : '-'}${s}%`
  return `${value < 0 ? '-' : ''}${s}%`
}

export function fmtNumber(value: number): string {
  return decimalFmt.format(value)
}

export function fmtMultiplier(value: number): string {
  return `${decimalFmt.format(value)}×`
}
