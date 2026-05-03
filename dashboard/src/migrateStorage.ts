// One-shot localStorage rename when the project moved from `bullish_scanner`
// to `tradepilot`. Copies any `bullish.*` / `bullish-*` keys to their
// `tradepilot.*` / `tradepilot-*` equivalents and deletes the originals.
//
// Idempotent: subsequent runs are no-ops once the old keys are gone. Safe to
// remove this whole file (and its call in main.tsx) once enough time has
// passed that you're confident no users are still on a pre-rename build.

const DIRECT_RENAMES: Array<[string, string]> = [
  ['bullish.selected_account', 'tradepilot.selected_account'],
  ['bullish.result_filters', 'tradepilot.result_filters'],
  ['bullish.risk_pct', 'tradepilot.risk_pct'],
  ['bullish.chart_indicators', 'tradepilot.chart_indicators'],
  // react-resizable-panels persists under `react-resizable-panels:<autoSaveId>`
  ['react-resizable-panels:bullish-mainsplit', 'react-resizable-panels:tradepilot-mainsplit'],
  ['react-resizable-panels:bullish-rightrail', 'react-resizable-panels:tradepilot-rightrail'],
]

const PREFIX_RENAMES: Array<[string, string]> = [
  // Per-ticker chart interval: `bullish.chart_interval.AAPL` → `tradepilot.chart_interval.AAPL`
  ['bullish.chart_interval.', 'tradepilot.chart_interval.'],
]

function move(oldKey: string, newKey: string): boolean {
  const value = window.localStorage.getItem(oldKey)
  if (value === null) return false
  // If the new key already has a value, the user must have written something
  // post-migration on another tab — keep that and just drop the legacy entry.
  if (window.localStorage.getItem(newKey) === null) {
    window.localStorage.setItem(newKey, value)
  }
  window.localStorage.removeItem(oldKey)
  return true
}

export function migrateLegacyStorageKeys(): void {
  if (typeof window === 'undefined' || !window.localStorage) return

  let moved = 0
  for (const [oldKey, newKey] of DIRECT_RENAMES) {
    if (move(oldKey, newKey)) moved += 1
  }

  // Prefix sweep — collect matching keys first because we mutate during iteration.
  for (const [oldPrefix, newPrefix] of PREFIX_RENAMES) {
    const matches: string[] = []
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const k = window.localStorage.key(i)
      if (k && k.startsWith(oldPrefix)) matches.push(k)
    }
    for (const oldKey of matches) {
      const newKey = newPrefix + oldKey.slice(oldPrefix.length)
      if (move(oldKey, newKey)) moved += 1
    }
  }

  if (moved > 0) {
    // Useful breadcrumb in DevTools the first time a user loads the renamed build.
    console.info(`[tradepilot] migrated ${moved} legacy localStorage key(s)`)
  }
}
