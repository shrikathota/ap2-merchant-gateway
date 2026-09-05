export const EVENT_META = {
  INTENT_VERIFIED: { icon: '🔑', color: 'border-sky-500/50 bg-sky-500/10 text-sky-300', dot: 'bg-sky-400' },
  CART_VERIFIED: { icon: '🛒', color: 'border-indigo-500/50 bg-indigo-500/10 text-indigo-300', dot: 'bg-indigo-400' },
  BUDGET_PASSED: { icon: '💰', color: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-300', dot: 'bg-emerald-400' },
  POLICY_PASSED: { icon: '🛡️', color: 'border-teal-500/50 bg-teal-500/10 text-teal-300', dot: 'bg-teal-400' },
  ORDER_CREATED: { icon: '📦', color: 'border-amber-500/50 bg-amber-500/10 text-amber-300', dot: 'bg-amber-400' },
  SETTLED: { icon: '✅', color: 'border-green-500/50 bg-green-500/10 text-green-300', dot: 'bg-green-400' },
  FAILURE_DIVERTED: { icon: '🔀', color: 'border-rose-500/50 bg-rose-500/10 text-rose-300', dot: 'bg-rose-400' },
  CAPTURE_REJECTED: { icon: '🚫', color: 'border-red-500/50 bg-red-500/10 text-red-300', dot: 'bg-red-400' },
}

export const DEFAULT_META = {
  icon: '•',
  color: 'border-zinc-600/50 bg-zinc-600/10 text-zinc-300',
  dot: 'bg-zinc-400',
}

export function metaFor(eventType) {
  return EVENT_META[eventType] ?? DEFAULT_META
}
