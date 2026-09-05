function formatPaise(paise) {
  return `₹${(paise / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`
}

export default function AlternativesList({ alternatives }) {
  if (!alternatives || alternatives.length === 0) return null

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">
        Offered alternatives
      </p>
      <ul className="space-y-1.5">
        {alternatives.map((alt) => (
          <li
            key={alt.sku}
            className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-xs"
          >
            <div className="flex items-center gap-2">
              <span className="font-mono font-semibold text-zinc-100">{alt.sku}</span>
              <span className="text-zinc-500">{alt.name}</span>
              {alt.is_upsell && (
                <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-2 py-0.5 font-medium text-amber-300 ring-1 ring-inset ring-amber-500/30">
                  💰 upsell +{formatPaise(alt.revenue_delta_paise)}
                </span>
              )}
            </div>
            <div className="flex items-center gap-3 text-zinc-400">
              <span>{formatPaise(alt.price_paise)}</span>
              <span>stock {alt.stock_qty}</span>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
