import '@testing-library/jest-dom/vitest'

// Recharts uses ResponsiveContainer which needs layout measurement; polyfill
// ResizeObserver in jsdom so the smoke tests don't crash on mount.
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  (globalThis as unknown as { ResizeObserver?: typeof ResizeObserverMock }).ResizeObserver ??
  ResizeObserverMock
