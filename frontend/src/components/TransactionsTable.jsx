import StatusBadge from './StatusBadge'

function formatPaise(paise) {
  return `₹${(paise / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`
}

function formatTime(ts) {
  return new Date(ts).toLocaleString(undefined, { hour12: false })
}

export default function TransactionsTable({ transactions, selectedIntentId, onSelect }) {
  if (!transactions || transactions.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-zinc-800 p-8 text-center text-sm text-zinc-500">
        No transactions yet. Run a POST /api/transact flow to see it here.
      </div>
    )
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-zinc-800">
      <table className="w-full min-w-[720px] text-left text-sm">
        <thead className="bg-zinc-900/80 text-xs uppercase tracking-wide text-zinc-500">
          <tr>
            <th className="px-4 py-2.5 font-medium">Status</th>
            <th className="px-4 py-2.5 font-medium">Order ID</th>
            <th className="px-4 py-2.5 font-medium">Amount</th>
            <th className="px-4 py-2.5 font-medium">Agent</th>
            <th className="px-4 py-2.5 font-medium">Intent</th>
            <th className="px-4 py-2.5 font-medium">Updated</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-800">
          {transactions.map((txn) => {
            const active = txn.intent_id === selectedIntentId
            return (
              <tr
                key={txn.razorpay_order_id}
                onClick={() => onSelect(txn.intent_id)}
                className={`cursor-pointer transition ${
                  active ? 'bg-violet-500/10' : 'hover:bg-zinc-900/60'
                }`}
              >
                <td className="px-4 py-2.5">
                  <StatusBadge status={txn.status} />
                </td>
                <td className="px-4 py-2.5 font-mono text-xs text-zinc-300">{txn.razorpay_order_id}</td>
                <td className="px-4 py-2.5 text-zinc-300">{formatPaise(txn.amount_paise)}</td>
                <td className="px-4 py-2.5 text-zinc-400">{txn.agent_id}</td>
                <td className="px-4 py-2.5 font-mono text-xs text-zinc-500">
                  {txn.intent_id.slice(0, 8)}…
                </td>
                <td className="px-4 py-2.5 text-xs text-zinc-500">{formatTime(txn.updated_at)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
