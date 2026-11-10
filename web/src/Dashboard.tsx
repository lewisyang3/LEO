import type { Dashboard, FindingSummary } from './api'
import { humanize, longDate, money } from './api'
import { findingHref } from './route'
import { useFetch } from './useFetch'

/** Days until the recommended send-by date. `long` spells it out for headers. */
export function DaysLeft({ days, long = false }: { days: number | null; long?: boolean }) {
  if (days === null) return <span className="muted">no deadline</span>
  const cls = days < 14 ? 'urgent' : days < 30 ? 'soon' : ''
  const text =
    days < 0
      ? `${-days}d overdue`
      : long
        ? `${days} ${days === 1 ? 'day' : 'days'} to send`
        : `${days}d`
  return (
    <span className={`days ${cls}`} title="Days until the recommended send-by date">
      {text}
    </span>
  )
}

export function StateBadge({ state }: { state: string }) {
  return <span className={`badge state-${state}`}>{state}</span>
}

export default function DashboardPage({ dash, version }: { dash: Dashboard | null; version: number }) {
  const { data: rows } = useFetch<FindingSummary[]>('/api/findings', version)

  return (
    <>
      <section className="totals">
        <div>
          <strong>{dash?.open_findings ?? '…'}</strong>
          <span>open findings</span>
        </div>
        <div>
          <strong>{dash ? money(dash.at_stake) : '…'}</strong>
          <span>at stake</span>
        </div>
        <div>
          <strong>{dash?.pending_actions ?? '…'}</strong>
          <span>drafts awaiting approval</span>
        </div>
      </section>

      <table className="findings">
        <thead>
          <tr>
            <th>Days to send</th>
            <th>Vendor</th>
            <th>Finding</th>
            <th>Send by</th>
            <th>Deadline</th>
            <th className="num">At stake</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {rows?.map((r) => (
            <tr key={r.id} onClick={() => (window.location.hash = findingHref(r.id))}>
              <td>
                <DaysLeft days={r.days_left} />
              </td>
              <td>{r.vendor_name}</td>
              <td>
                <a href={findingHref(r.id)}>{r.title}</a>
                <div className="muted small">
                  {humanize(r.kind)}
                  {r.plan_label && ` · ${r.plan_label}`}
                </div>
              </td>
              <td>{longDate(r.recommended_send_by)}</td>
              <td>{longDate(r.deadline)}</td>
              <td className="num">{money(r.at_stake)}</td>
              <td>
                <StateBadge state={r.action_state} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
