import { useCallback, useEffect, useState } from 'react'
import { get } from './api'

/** GET `url` on mount and whenever `url` or `version` changes. */
export function useFetch<T>(url: string | null, version = 0) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  useEffect(() => {
    if (url === null) return
    let live = true
    get<T>(url).then(
      (d) => live && (setData(d), setError(null)),
      (e: Error) => live && setError(e.message),
    )
    return () => {
      live = false
    }
  }, [url, version, tick])
  const reload = useCallback(() => setTick((t) => t + 1), [])
  return { data, error, reload }
}
