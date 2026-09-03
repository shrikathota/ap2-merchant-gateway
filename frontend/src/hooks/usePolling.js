import { useEffect, useRef, useState } from 'react'

/**
 * Poll `fetcher()` every `intervalMs`. Re-starts whenever `deps` change.
 * Keeps the last good `data` on screen while a poll is in flight or fails,
 * so the dashboard never flashes empty on a transient network hiccup.
 */
export function usePolling(fetcher, deps, intervalMs = 2000) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  useEffect(() => {
    let cancelled = false
    setLoading(true)

    async function tick() {
      try {
        const result = await fetcherRef.current()
        if (!cancelled) {
          setData(result)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) setError(err)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    tick()
    const id = setInterval(tick, intervalMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, loading }
}
