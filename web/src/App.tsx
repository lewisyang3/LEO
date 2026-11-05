import { useEffect, useState } from 'react'

type Health = { status: string; db: string }

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch('/api/health')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setHealth)
      .catch((e: Error) => setError(e.message))
  }, [])

  return (
    <main>
      <h1>LEO</h1>
      {error && <p>API unreachable: {error}. Is `make api` running?</p>}
      {health && (
        <p>
          API: {health.status} · DB: {health.db}
        </p>
      )}
      {!health && !error && <p>Checking API…</p>}
    </main>
  )
}
