import { useState } from 'react'
import type { Dashboard, FindingSummary, Stats } from './api'
import { humanize, longDate, money, upload } from './api'
import { findingHref, vendorHref } from './route'
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

export function StateBadge({ state }: { state: string | null }) {
  if (state === null) return <span className="badge">no draft</span>
  return <span className={`badge state-${state}`}>{state}</span>
}

/** The stage funnel: how many documents each stage let through, and which engine extracted
 * them. Counts come from the jobs table, so cache hits count like live calls. */
function Funnel({ version }: { version: number }) {
  const { data: s } = useFetch<Stats>('/api/stats', version)
  if (!s || s.documents === 0) return null
  const noise = s.documents_by_class.noise ?? 0
  const extracts = s.jobs.filter((j) => j.stage === 'extract' && j.status === 'done')
  const scripts = new Map<string, number>()
  let llm = 0
  for (const j of extracts) {
    const by = j.processed_by ?? ''
    if (by.startsWith('script:')) {
      const name = by.slice('script:'.length).replace(/@.*/, '')
      scripts.set(name, (scripts.get(name) ?? 0) + j.count)
    } else if (by.startsWith('llm:')) llm += j.count
  }
  const scripted = [...scripts.values()].reduce((a, b) => a + b, 0)
  const step = (n: number, label: string, title?: string) => (
    <div title={title}>
      <strong>{n}</strong>
      <span>{label}</span>
    </div>
  )
  return (
    <section className="funnel" aria-label="Stage funnel">
      {step(s.documents, 'documents in')}
      <span className="arrow">→</span>
      {step(noise, 'stopped as noise')}
      <span className="arrow">→</span>
      {step(scripted, 'read by scripts', [...scripts].map(([n, c]) => `${n}: ${c}`).join(', '))}
      <span className="arrow">+</span>
      {step(llm, 'reached the LLM')}
      {scripts.size > 0 && (
        <p className="muted small">Scripts: {[...scripts.keys()].join(', ')}</p>
      )}
    </section>
  )
}

/** Upload one document. The worker processes it. */
function Upload() {
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ text: string; error?: boolean } | null>(null)

  async function pick(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = '' // picking the same file again still fires
    if (!file) return
    setBusy(true)
    try {
      const r = await upload(file)
      setNote({
        text: r.created
          ? `Stored as ${r.path}. Processing arrives with the worker.`
          : `Already ingested, as ${r.path}.`,
      })
    } catch (err) {
      setNote({ text: `Upload failed: ${(err as Error).message}`, error: true })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="upload">
      <label>
        <span className="muted small">Upload a document (.eml or contract) </span>
        <input type="file" onChange={pick} disabled={busy} />
      </label>
      {note && <p className={note.error ? 'error small' : 'muted small'}>{note.text}</p>}
    </section>
  )
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

      <Funnel version={version} />

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
              <td>
                <a href={vendorHref(r.vendor)} onClick={(e) => e.stopPropagation()}>
                  {r.vendor_name}
                </a>
              </td>
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

      <p className="export muted small">
        Export CSV: <a href="/api/export/terms.csv" download>term record</a> ·{' '}
        <a href="/api/export/findings.csv" download>findings</a>
      </p>

      <Upload />
    </>
  )
}
