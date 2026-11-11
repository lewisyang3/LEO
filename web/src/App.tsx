import { useCallback, useEffect, useState } from 'react'
import type { Dashboard } from './api'
import ApprovalQueue from './ApprovalQueue'
import DashboardPage from './Dashboard'
import FindingDetail from './FindingDetail'
import VendorDetail from './VendorDetail'
import Vendors from './Vendors'
import { useHashRoute } from './route'
import { useFetch } from './useFetch'

const POLL_MS = 5000

export default function App() {
  const route = useHashRoute()
  // Bumped after any action change, so the header count and lists refetch.
  const [version, setVersion] = useState(0)
  const changed = useCallback(() => setVersion((v) => v + 1), [])
  // The findings pages refetch every few seconds, so a finding the worker adds or moves (a
  // synced email, a correction's reconcile) shows up without a reload. Forms keep their state.
  const [poll, setPoll] = useState(0)
  const polling = route.page === 'findings' || route.page === 'finding'
  useEffect(() => {
    if (!polling) return
    const t = setInterval(() => setPoll((p) => p + 1), POLL_MS)
    return () => clearInterval(t)
  }, [polling])
  const fresh = version + poll
  const { data: dash, error } = useFetch<Dashboard>('/api/dashboard', fresh)

  return (
    <>
      <header className="topbar">
        <a href="#/" className="brand">
          LEO
        </a>
        <nav>
          <a href="#/" className={route.page === 'findings' || route.page === 'finding' ? 'active' : ''}>
            Findings
          </a>
          <a href="#/vendors" className={route.page === 'vendors' || route.page === 'vendor' ? 'active' : ''}>
            Vendors
          </a>
          <a href="#/actions" className={route.page === 'actions' ? 'active' : ''}>
            Approvals{dash ? ` (${dash.pending_actions})` : ''}
          </a>
        </nav>
        <span className="today">{dash && `Today ${dash.today}`}</span>
      </header>
      <main>
        {error && <p className="error">API unreachable: {error}. Is `make api` running?</p>}
        {route.page === 'findings' && <DashboardPage dash={dash} version={fresh} />}
        {route.page === 'finding' && <FindingDetail key={route.id} id={route.id} version={fresh} />}
        {route.page === 'vendors' && <Vendors version={version} />}
        {route.page === 'vendor' && <VendorDetail key={route.id} id={route.id} version={version} />}
        {route.page === 'actions' && <ApprovalQueue version={version} onChange={changed} />}
      </main>
    </>
  )
}
