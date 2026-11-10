import type { VendorSummary } from './api'
import { money } from './api'
import { vendorHref } from './route'
import { useFetch } from './useFetch'

/** Every vendor, most at stake first, so the ones that need attention lead. */
export default function Vendors({ version }: { version: number }) {
  const { data, error } = useFetch<VendorSummary[]>('/api/vendors', version)
  if (error) return <p className="error">{error}</p>
  if (!data) return <p className="muted">Loading…</p>

  const rows = [...data].sort(
    (a, b) =>
      b.at_stake - a.at_stake ||
      b.open_findings - a.open_findings ||
      a.canonical_name.localeCompare(b.canonical_name),
  )
  return (
    <>
      <h1>Vendors</h1>
      <p className="muted">
        {rows.length} vendors. Open one for its findings, price history, contract terms and card
        charges.
      </p>
      <table className="rows vendors">
        <thead>
          <tr>
            <th>Vendor</th>
            <th className="num">Open findings</th>
            <th className="num">At stake</th>
            <th className="num">Terms</th>
            <th className="num">Card charges</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((v) => (
            <tr
              key={v.id}
              className={v.terms === 0 && v.open_findings === 0 ? 'quiet' : ''}
              onClick={() => (window.location.hash = vendorHref(v.id))}
            >
              <td>
                <a href={vendorHref(v.id)}>{v.canonical_name}</a>
                {v.aliases.length > 0 && (
                  <div className="muted small">Also seen as {v.aliases.join(', ')}</div>
                )}
              </td>
              <td className="num">{v.open_findings || <span className="muted">—</span>}</td>
              <td className="num">
                {v.at_stake > 0 ? money(v.at_stake) : <span className="muted">—</span>}
              </td>
              <td className="num">
                {v.terms || <span className="muted small">no contract on file</span>}
              </td>
              <td className="num">{v.transactions}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
