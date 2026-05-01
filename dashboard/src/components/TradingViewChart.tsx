import { useEffect, useRef } from 'react'

declare global {
  interface Window {
    TradingView?: {
      widget: new (config: Record<string, unknown>) => unknown
    }
  }
}

const SCRIPT_SRC = 'https://s3.tradingview.com/tv.js'

let scriptPromise: Promise<void> | null = null
let widgetCounter = 0

function loadTradingViewScript(): Promise<void> {
  if (scriptPromise) return scriptPromise
  scriptPromise = new Promise((resolve, reject) => {
    if (window.TradingView) {
      resolve()
      return
    }
    const existing = document.querySelector(
      `script[src="${SCRIPT_SRC}"]`,
    ) as HTMLScriptElement | null
    if (existing) {
      existing.addEventListener('load', () => resolve())
      existing.addEventListener('error', () =>
        reject(new Error('Failed to load TradingView script')),
      )
      return
    }
    const s = document.createElement('script')
    s.src = SCRIPT_SRC
    s.async = true
    s.onload = () => resolve()
    s.onerror = () => reject(new Error('Failed to load TradingView script'))
    document.head.appendChild(s)
  })
  return scriptPromise
}

interface Props {
  ticker: string
  height?: number | 'fill'
}

export function TradingViewChart({ ticker, height = 460 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const idRef = useRef<string>(`tv-chart-${++widgetCounter}`)

  useEffect(() => {
    let cancelled = false
    const id = idRef.current
    const isDark =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-color-scheme: dark)').matches

    loadTradingViewScript()
      .then(() => {
        if (cancelled || !containerRef.current || !window.TradingView) return
        containerRef.current.innerHTML = ''
        const target = document.createElement('div')
        target.id = id
        target.style.height = '100%'
        target.style.width = '100%'
        containerRef.current.appendChild(target)
        new window.TradingView.widget({
          autosize: true,
          symbol: ticker,
          interval: '1',
          timezone: 'America/New_York',
          theme: isDark ? 'dark' : 'light',
          style: '1',
          locale: 'en',
          hide_side_toolbar: false,
          allow_symbol_change: true,
          studies: ['MAExp@tv-basicstudies', 'RSI@tv-basicstudies'],
          studies_overrides: {
            'moving average exponential.length': 9,
            'relative strength index.length': 9,
          },
          container_id: id,
        })
      })
      .catch((e) => {
        console.warn('TradingView widget failed to load', e)
      })

    return () => {
      cancelled = true
      if (containerRef.current) containerRef.current.innerHTML = ''
    }
  }, [ticker])

  const fill = height === 'fill'
  const style = fill ? { height: '100%' } : { height }
  return (
    <div
      style={style}
      className="w-full overflow-hidden rounded border border-neutral-200 dark:border-neutral-800"
    >
      <div ref={containerRef} className="h-full w-full" />
    </div>
  )
}
