import { useEffect, useState } from 'react'
import { fetchAuditChain, fetchLatestFlow, fetchTransactions } from './api'
import Timeline from './components/Timeline'
import TransactionsTable from './components/TransactionsTable'
import { usePolling } from './hooks/usePolling'

const POLL_MS = 2000

export default function App() {
  const [followLatest, setFollowLatest] = useState(true)
  const [selectedIntentId, setSelectedIntentId] = useState(null)

  const { data: latest } = usePolling(fetchLatestFlow, [], POLL_MS)
  const { data: transactions, error: txnError } = usePolling(fetchTransactions, [], POLL_MS)

  const effectiveIntentId = followLatest ? latest?.intent_id ?? null : selectedIntentId

  const {
    data: chain,
    error: chainError,
    loading: chainLoading,
  } = usePolling(
    () => (effectiveIntentId ? fetchAuditChain(effectiveIntentId) : Promise.resolve(null)),
    [effectiveIntentId],
    POLL_MS,
  )

  // Follow-latest mode: if the latest flow changes (a new transaction started
  // elsewhere), snap to it automatically.
  useEffect(() => {
    if (followLatest && latest?.intent_id) {
      setSelectedIntentId(latest.intent_id)
    }
  }, [followLatest, latest?.intent_id])

  function handleSelect(intentId) {
    setFollowLatest(false)
    setSelectedIntentId(intentId)
  }

  function handleReplayLatest() {
    setFollowLatest(true)
  }

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <header className="border-b border-zinc-800 bg-zinc-950/80 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
          <div>
            <h1 className="text-lg font-semibold tracking-tight">AP2 Merchant Gateway — Audit Dashboard</h1>
            <p className="text-xs text-zinc-500">Append-only ledger, polled every 2s</p>
          </div>
          <div className="flex items-center gap-2 text-xs text-zinc-500">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
            </span>
            live
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl space-y-8 px-6 py-8">
        <section>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">Transactions</h2>
            {txnError && <span className="text-xs text-rose-400">failed to load: {txnError.message}</span>}
          </div>
          <TransactionsTable
            transactions={transactions}
            selectedIntentId={effectiveIntentId}
            onSelect={handleSelect}
          />
        </section>

        <section>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">Event Chain</h2>
              {effectiveIntentId && (
                <span className="rounded-full bg-zinc-800 px-2 py-0.5 font-mono text-xs text-zinc-400">
                  {effectiveIntentId}
                </span>
              )}
              {followLatest && (
                <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-300 ring-1 ring-inset ring-emerald-500/30">
                  following latest
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={handleReplayLatest}
              disabled={followLatest}
              className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-200 transition hover:border-violet-500 hover:text-violet-300 disabled:cursor-default disabled:opacity-40 disabled:hover:border-zinc-700 disabled:hover:text-zinc-200"
            >
              ↻ Replay latest
            </button>
          </div>

          {chainError && <p className="mb-2 text-xs text-rose-400">failed to load chain: {chainError.message}</p>}
          {!effectiveIntentId && !chainLoading && (
            <div className="rounded-xl border border-dashed border-zinc-800 p-8 text-center text-sm text-zinc-500">
              No transaction flows yet. Run one against POST /api/transact.
            </div>
          )}
          {effectiveIntentId && <Timeline events={chain?.events} />}
        </section>
      </main>
    </div>
  )
}
