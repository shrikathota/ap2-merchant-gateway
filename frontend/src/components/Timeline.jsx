import { useState } from 'react'
import { metaFor } from '../eventMeta'
import JsonPayload from './JsonPayload'

function formatTime(ts) {
  const d = new Date(ts)
  return d.toLocaleTimeString(undefined, { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0')
}

function EventRow({ event, isLast }) {
  const [open, setOpen] = useState(false)
  const meta = metaFor(event.event_type)

  return (
    <li className="relative pb-6 pl-12">
      {!isLast && (
        <span className="absolute left-[19px] top-9 h-full w-px bg-zinc-700" aria-hidden="true" />
      )}
      <span
        className={`absolute left-0 flex h-10 w-10 items-center justify-center rounded-full border text-lg ${meta.color}`}
      >
        {meta.icon}
      </span>

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-3 rounded-lg border border-zinc-800 bg-zinc-900/60 px-4 py-2.5 text-left transition hover:border-zinc-700 hover:bg-zinc-900"
      >
        <div className="flex items-center gap-3">
          <span className="font-mono text-sm font-semibold text-zinc-100">{event.event_type}</span>
          {event.mandate_id && (
            <span className="hidden truncate font-mono text-xs text-zinc-500 sm:inline">
              {event.mandate_id.slice(0, 8)}…
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          <span className="font-mono text-xs text-zinc-500">{formatTime(event.timestamp)}</span>
          <span className={`text-xs text-zinc-500 transition-transform ${open ? 'rotate-90' : ''}`}>▶</span>
        </div>
      </button>

      {open && (
        <div className="mt-2 ml-1 space-y-2 rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs text-zinc-400">
            <dt className="text-zinc-500">event id</dt>
            <dd className="font-mono">{event.id}</dd>
            <dt className="text-zinc-500">agent_id</dt>
            <dd className="font-mono">{event.agent_id ?? '—'}</dd>
            <dt className="text-zinc-500">mandate_id</dt>
            <dd className="font-mono break-all">{event.mandate_id ?? '—'}</dd>
          </dl>
          <JsonPayload value={event.payload_snapshot} />
        </div>
      )}
    </li>
  )
}

export default function Timeline({ events }) {
  if (!events || events.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-zinc-800 p-8 text-center text-sm text-zinc-500">
        No audit events yet for this flow.
      </div>
    )
  }

  return (
    <ol className="mt-2">
      {events.map((event, i) => (
        <EventRow key={event.id} event={event} isLast={i === events.length - 1} />
      ))}
    </ol>
  )
}
