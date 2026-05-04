import { useEffect, useMemo, useState } from 'react'
import {
  connectBroker,
  createConnection,
  deleteConnection,
  disconnectBroker,
  fetchAccountAliases,
  fetchAccountsSummary,
  fetchConnections,
  saveAccountAliases,
  updateConnection,
  type ConnectionUpsert,
} from '../api'
import { fmtCurrency } from '../format'
import type { AccountSummaryRow, ConnectionInfo } from '../types'

/** Full-page Connect view: manage TWS connections (top) and view all
 * accounts across them with editable aliases (bottom). Modeled after
 * NanoPulse's Connect tab.
 *
 * Polling cadence: connections every 4 s (cheap call), account
 * summaries every 8 s (one IB round-trip per connection — pricier but
 * still fine at small connection counts). The refresh is also
 * triggered manually after any mutation.
 */
export function ConnectView({ onError }: { onError: (msg: string) => void }) {
  const [connections, setConnections] = useState<ConnectionInfo[]>([])
  const [accounts, setAccounts] = useState<AccountSummaryRow[]>([])
  const [aliases, setAliases] = useState<Record<string, string>>({})
  const [draftAliases, setDraftAliases] = useState<Record<string, string>>({})
  const [search, setSearch] = useState<string>('')
  const [editingLabel, setEditingLabel] = useState<string | null>(null)
  const [draftConn, setDraftConn] = useState<ConnectionUpsert | null>(null)
  const [pendingAction, setPendingAction] = useState<string | null>(null)

  const refresh = async () => {
    try {
      const [c, a, al] = await Promise.all([
        fetchConnections(),
        fetchAccountsSummary().catch(() => ({ accounts: [], errors: [] })),
        fetchAccountAliases().catch(() => ({})),
      ])
      setConnections(c)
      setAccounts(a.accounts)
      setAliases(al)
      // Don't clobber an in-flight draft alias the user is typing.
      setDraftAliases((prev) => ({ ...al, ...prev }))
    } catch (e) {
      onError(String(e))
    }
  }

  useEffect(() => {
    void refresh()
    const id = window.setInterval(() => void refresh(), 4_000)
    return () => window.clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const startEdit = (c: ConnectionInfo) => {
    setEditingLabel(c.label)
    setDraftConn({
      label: c.label,
      host: c.host,
      port: c.port,
      client_id: c.client_id,
      paper: c.paper,
      auto_connect: c.auto_connect,
      default_account: c.default_account ?? null,
    })
  }

  const startAdd = () => {
    // Pick a non-colliding client_id by default — first integer not used.
    const used = new Set(connections.map((c) => c.client_id))
    let next = 100
    while (used.has(next)) next += 1
    setEditingLabel('__new__')
    setDraftConn({
      label: `conn-${connections.length + 1}`,
      host: '127.0.0.1',
      port: 7497,
      client_id: next,
      paper: true,
      auto_connect: true,
      default_account: null,
    })
  }

  const cancelEdit = () => {
    setEditingLabel(null)
    setDraftConn(null)
  }

  const saveEdit = async () => {
    if (!draftConn || !editingLabel) return
    setPendingAction('save')
    try {
      if (editingLabel === '__new__') {
        await createConnection(draftConn)
      } else {
        await updateConnection(editingLabel, draftConn)
      }
      cancelEdit()
      await refresh()
    } catch (e) {
      onError(String(e))
    } finally {
      setPendingAction(null)
    }
  }

  const doDelete = async (label: string) => {
    if (!window.confirm(`Delete connection "${label}"? This stops it and removes it from the config.`)) return
    setPendingAction(`del-${label}`)
    try {
      await deleteConnection(label)
      await refresh()
    } catch (e) {
      onError(String(e))
    } finally {
      setPendingAction(null)
    }
  }

  const doConnect = async (label: string) => {
    setPendingAction(`conn-${label}`)
    try {
      await connectBroker(label)
      await refresh()
    } catch (e) {
      onError(String(e))
    } finally {
      setPendingAction(null)
    }
  }

  const doDisconnect = async (label: string) => {
    setPendingAction(`disc-${label}`)
    try {
      await disconnectBroker(label)
      await refresh()
    } catch (e) {
      onError(String(e))
    } finally {
      setPendingAction(null)
    }
  }

  const connectAll = async () => {
    setPendingAction('connect-all')
    try {
      for (const c of connections) {
        if (!c.connected) await connectBroker(c.label).catch(() => null)
      }
      await refresh()
    } finally {
      setPendingAction(null)
    }
  }

  const disconnectAll = async () => {
    setPendingAction('disconnect-all')
    try {
      for (const c of connections) {
        if (c.connected) await disconnectBroker(c.label).catch(() => null)
      }
      await refresh()
    } finally {
      setPendingAction(null)
    }
  }

  const commitAlias = async (account: string) => {
    const next = (draftAliases[account] ?? '').trim()
    if (next === (aliases[account] ?? '')) return
    try {
      const merged = { ...aliases, [account]: next }
      // Drop empty values so blank aliases get removed from the file.
      Object.keys(merged).forEach((k) => {
        if (!merged[k]) delete merged[k]
      })
      const saved = await saveAccountAliases(merged)
      setAliases(saved)
    } catch (e) {
      onError(String(e))
    }
  }

  // Filtered + sorted accounts. Search matches connection / account / alias.
  const filteredAccounts = useMemo(() => {
    const s = search.trim().toLowerCase()
    if (!s) return accounts
    return accounts.filter(
      (r) =>
        r.account.toLowerCase().includes(s) ||
        r.connection.toLowerCase().includes(s) ||
        (r.alias ?? '').toLowerCase().includes(s),
    )
  }, [accounts, search])

  const totals = useMemo(() => {
    return filteredAccounts.reduce(
      (acc, r) => ({
        net_liq: acc.net_liq + r.net_liquidation,
        cash: acc.cash + r.total_cash,
        excess: acc.excess + r.excess_liquidity,
        daily: acc.daily + r.daily_pnl,
      }),
      { net_liq: 0, cash: 0, excess: 0, daily: 0 },
    )
  }, [filteredAccounts])

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto bg-neutral-950 p-3 text-xs text-neutral-200">
      {/* Connections section */}
      <section>
        <h2 className="mb-1 text-[11px] uppercase tracking-wide text-neutral-400">Connections</h2>
        <div className="overflow-x-auto rounded border border-neutral-800">
          <table className="w-full text-xs">
            <thead className="bg-neutral-900/60 text-[10px] uppercase tracking-wide text-neutral-500">
              <tr>
                <th className="px-2 py-1 text-left">Label</th>
                <th className="px-2 py-1 text-left">Host</th>
                <th className="px-2 py-1 text-left">Port</th>
                <th className="px-2 py-1 text-left">Client&nbsp;ID</th>
                <th className="px-2 py-1 text-left">Type</th>
                <th className="px-2 py-1 text-center">Auto&nbsp;Start</th>
                <th className="px-2 py-1 text-left">Status</th>
                <th className="px-2 py-1 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {connections.map((c) => {
                const isEditing = editingLabel === c.label
                const draft = isEditing ? draftConn : null
                return (
                  <tr key={c.label} className="border-t border-neutral-800 hover:bg-neutral-900/40">
                    <Cell>
                      {isEditing && draft ? (
                        <Input
                          value={draft.label}
                          onChange={(v) => setDraftConn({ ...draft, label: v })}
                        />
                      ) : (
                        <span className="font-semibold">{c.label}</span>
                      )}
                    </Cell>
                    <Cell>
                      {isEditing && draft ? (
                        <Input
                          value={draft.host}
                          onChange={(v) => setDraftConn({ ...draft, host: v })}
                        />
                      ) : (
                        c.host
                      )}
                    </Cell>
                    <Cell>
                      {isEditing && draft ? (
                        <Input
                          value={String(draft.port)}
                          type="number"
                          onChange={(v) => setDraftConn({ ...draft, port: Number(v) })}
                        />
                      ) : (
                        c.port
                      )}
                    </Cell>
                    <Cell>
                      {isEditing && draft ? (
                        <Input
                          value={String(draft.client_id)}
                          type="number"
                          onChange={(v) => setDraftConn({ ...draft, client_id: Number(v) })}
                        />
                      ) : (
                        c.client_id
                      )}
                    </Cell>
                    <Cell>
                      <span className={c.paper ? 'text-[var(--color-warn)]' : 'text-[var(--color-danger)]'}>
                        {c.paper ? 'Paper' : 'Live'}
                      </span>
                    </Cell>
                    <Cell center>
                      {isEditing && draft ? (
                        <input
                          type="checkbox"
                          checked={draft.auto_connect}
                          onChange={(e) => setDraftConn({ ...draft, auto_connect: e.target.checked })}
                          className="h-3 w-3 cursor-pointer accent-[var(--color-accent)]"
                        />
                      ) : (
                        <span className={c.auto_connect ? 'text-[var(--color-accent-dim)]' : 'text-neutral-500'}>
                          {c.auto_connect ? 'On' : 'Off'}
                        </span>
                      )}
                    </Cell>
                    <Cell>
                      {c.connected ? (
                        <span className="text-[var(--color-accent-dim)]">Connected</span>
                      ) : (
                        <span className="text-[var(--color-danger)]">Disconnected</span>
                      )}
                    </Cell>
                    <Cell right>
                      {isEditing && draft ? (
                        <span className="flex justify-end gap-1">
                          <SmallButton
                            label="Save"
                            color="accent"
                            disabled={pendingAction === 'save'}
                            onClick={saveEdit}
                          />
                          <SmallButton label="Cancel" color="neutral" onClick={cancelEdit} />
                        </span>
                      ) : (
                        <span className="flex justify-end gap-1">
                          <SmallButton label="Edit" color="neutral" onClick={() => startEdit(c)} />
                          {c.connected ? (
                            <SmallButton
                              label="Disconnect"
                              color="danger"
                              disabled={pendingAction === `disc-${c.label}`}
                              onClick={() => void doDisconnect(c.label)}
                            />
                          ) : (
                            <SmallButton
                              label="Connect"
                              color="accent"
                              disabled={pendingAction === `conn-${c.label}`}
                              onClick={() => void doConnect(c.label)}
                            />
                          )}
                          <SmallButton
                            label="Del"
                            color="danger-outline"
                            disabled={pendingAction === `del-${c.label}`}
                            onClick={() => void doDelete(c.label)}
                          />
                        </span>
                      )}
                    </Cell>
                  </tr>
                )
              })}
              {editingLabel === '__new__' && draftConn && (
                <tr className="border-t border-neutral-800 bg-neutral-900/40">
                  <Cell>
                    <Input value={draftConn.label} onChange={(v) => setDraftConn({ ...draftConn, label: v })} />
                  </Cell>
                  <Cell>
                    <Input value={draftConn.host} onChange={(v) => setDraftConn({ ...draftConn, host: v })} />
                  </Cell>
                  <Cell>
                    <Input
                      value={String(draftConn.port)}
                      type="number"
                      onChange={(v) => setDraftConn({ ...draftConn, port: Number(v) })}
                    />
                  </Cell>
                  <Cell>
                    <Input
                      value={String(draftConn.client_id)}
                      type="number"
                      onChange={(v) => setDraftConn({ ...draftConn, client_id: Number(v) })}
                    />
                  </Cell>
                  <Cell>
                    <select
                      value={draftConn.paper ? 'paper' : 'live'}
                      onChange={(e) => setDraftConn({ ...draftConn, paper: e.target.value === 'paper' })}
                      className="rounded border border-neutral-700 bg-neutral-900 px-1 py-0.5 text-xs"
                    >
                      <option value="paper">Paper</option>
                      <option value="live">Live</option>
                    </select>
                  </Cell>
                  <Cell center>
                    <input
                      type="checkbox"
                      checked={draftConn.auto_connect}
                      onChange={(e) => setDraftConn({ ...draftConn, auto_connect: e.target.checked })}
                      className="h-3 w-3 cursor-pointer accent-[var(--color-accent)]"
                    />
                  </Cell>
                  <Cell>
                    <span className="text-neutral-500">— new —</span>
                  </Cell>
                  <Cell right>
                    <span className="flex justify-end gap-1">
                      <SmallButton
                        label="Save"
                        color="accent"
                        disabled={pendingAction === 'save'}
                        onClick={saveEdit}
                      />
                      <SmallButton label="Cancel" color="neutral" onClick={cancelEdit} />
                    </span>
                  </Cell>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="mt-2 flex gap-2">
          <SmallButton label="Add" color="neutral" onClick={startAdd} disabled={editingLabel !== null} />
          <SmallButton
            label="Connect All"
            color="accent"
            onClick={() => void connectAll()}
            disabled={pendingAction === 'connect-all' || connections.length === 0}
          />
          <SmallButton
            label="Disconnect All"
            color="danger"
            onClick={() => void disconnectAll()}
            disabled={pendingAction === 'disconnect-all' || connections.length === 0}
          />
        </div>
      </section>

      {/* Accounts section */}
      <section>
        <div className="mb-1 flex items-baseline justify-between">
          <h2 className="text-[11px] uppercase tracking-wide text-neutral-400">Accounts</h2>
          <input
            type="text"
            placeholder="Search…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-48 rounded border border-neutral-700 bg-neutral-900 px-2 py-0.5 text-xs"
          />
        </div>
        <div className="overflow-x-auto rounded border border-neutral-800">
          <table className="w-full text-xs">
            <thead className="bg-neutral-900/60 text-[10px] uppercase tracking-wide text-neutral-500">
              <tr>
                <th className="px-2 py-1 text-left">Connection</th>
                <th className="px-2 py-1 text-left">Account</th>
                <th className="px-2 py-1 text-left">Type</th>
                <th className="px-2 py-1 text-left">Alias</th>
                <th className="px-2 py-1 text-right">Net&nbsp;Liquidation</th>
                <th className="px-2 py-1 text-right">Total&nbsp;Cash</th>
                <th className="px-2 py-1 text-right">Excess&nbsp;Liq</th>
                <th className="px-2 py-1 text-right">Daily&nbsp;P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {filteredAccounts.map((r) => (
                <tr key={`${r.connection}::${r.account}`} className="border-t border-neutral-800 hover:bg-neutral-900/40">
                  <Cell>{r.connection}</Cell>
                  <Cell>
                    <span className="font-semibold num">{r.account}</span>
                  </Cell>
                  <Cell>
                    <span className={r.paper ? 'text-[var(--color-warn)]' : 'text-[var(--color-danger)]'}>
                      {r.paper ? 'Paper' : 'Live'}
                    </span>
                  </Cell>
                  <Cell>
                    <input
                      type="text"
                      value={draftAliases[r.account] ?? r.alias ?? ''}
                      placeholder="—"
                      onChange={(e) =>
                        setDraftAliases({ ...draftAliases, [r.account]: e.target.value })
                      }
                      onBlur={() => void commitAlias(r.account)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                      }}
                      className="w-24 rounded border border-neutral-700 bg-neutral-900 px-1.5 py-0.5 text-xs"
                    />
                  </Cell>
                  <Cell right mono>{fmtCurrency(r.net_liquidation)}</Cell>
                  <Cell right mono>{fmtCurrency(r.total_cash)}</Cell>
                  <Cell right mono>{fmtCurrency(r.excess_liquidity)}</Cell>
                  <Cell right mono>
                    <span className={r.daily_pnl >= 0 ? 'text-[var(--color-accent-dim)]' : 'text-[var(--color-danger)]'}>
                      {fmtCurrency(r.daily_pnl)}
                    </span>
                  </Cell>
                </tr>
              ))}
              {filteredAccounts.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-2 py-3 text-center text-neutral-500">
                    {accounts.length === 0
                      ? 'No accounts yet — connect a broker to populate.'
                      : 'No accounts match your search.'}
                  </td>
                </tr>
              )}
              {filteredAccounts.length > 0 && (
                <tr className="border-t border-neutral-800 bg-neutral-900/60 font-semibold">
                  <Cell>Total</Cell>
                  <Cell>—</Cell>
                  <Cell>—</Cell>
                  <Cell>—</Cell>
                  <Cell right mono>{fmtCurrency(totals.net_liq)}</Cell>
                  <Cell right mono>{fmtCurrency(totals.cash)}</Cell>
                  <Cell right mono>{fmtCurrency(totals.excess)}</Cell>
                  <Cell right mono>
                    <span className={totals.daily >= 0 ? 'text-[var(--color-accent-dim)]' : 'text-[var(--color-danger)]'}>
                      {fmtCurrency(totals.daily)}
                    </span>
                  </Cell>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}

function Cell({
  children,
  right,
  center,
  mono,
}: {
  children: React.ReactNode
  right?: boolean
  center?: boolean
  mono?: boolean
}) {
  const align = right ? 'text-right' : center ? 'text-center' : 'text-left'
  return (
    <td className={`px-2 py-1 ${align} ${mono ? 'num' : ''}`}>{children}</td>
  )
}

function Input({
  value,
  onChange,
  type = 'text',
}: {
  value: string
  onChange: (v: string) => void
  type?: 'text' | 'number'
}) {
  return (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full rounded border border-neutral-700 bg-neutral-900 px-1.5 py-0.5 text-xs"
    />
  )
}

function SmallButton({
  label,
  color,
  onClick,
  disabled,
}: {
  label: string
  color: 'accent' | 'danger' | 'neutral' | 'danger-outline'
  onClick: () => void
  disabled?: boolean
}) {
  const cls =
    color === 'accent'
      ? 'bg-[var(--color-accent)] text-neutral-950 hover:bg-[var(--color-accent)]/90'
      : color === 'danger'
        ? 'bg-[var(--color-danger)] text-white hover:bg-[var(--color-danger)]/90'
        : color === 'danger-outline'
          ? 'border border-[var(--color-danger)]/60 text-[var(--color-danger)] hover:bg-[var(--color-danger)]/10'
          : 'border border-neutral-700 bg-neutral-900 text-neutral-200 hover:border-neutral-500'
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${cls} disabled:cursor-not-allowed disabled:opacity-40`}
    >
      {label}
    </button>
  )
}
