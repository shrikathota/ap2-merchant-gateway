const STYLES = {
  APPROVED: 'bg-amber-500/15 text-amber-300 ring-amber-500/30',
  PENDING_PAYMENT: 'bg-amber-500/15 text-amber-300 ring-amber-500/30',
  SETTLED: 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30',
  FAILED: 'bg-rose-500/15 text-rose-300 ring-rose-500/30',
  ROLLED_BACK: 'bg-zinc-500/15 text-zinc-300 ring-zinc-500/30',
}

const LABELS = {
  PENDING_PAYMENT: 'APPROVED',
}

export default function StatusBadge({ status }) {
  const style = STYLES[status] ?? 'bg-zinc-500/15 text-zinc-300 ring-zinc-500/30'
  const label = LABELS[status] ?? status
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${style}`}
    >
      {label}
    </span>
  )
}
