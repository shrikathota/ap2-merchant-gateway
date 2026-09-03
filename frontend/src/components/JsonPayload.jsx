export default function JsonPayload({ value }) {
  if (value == null) {
    return <p className="text-xs italic text-zinc-500">no payload</p>
  }
  return (
    <pre className="max-h-80 overflow-auto rounded-lg bg-black/40 p-3 text-xs leading-relaxed text-zinc-300">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}
