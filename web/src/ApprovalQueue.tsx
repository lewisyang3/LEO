import { useState } from 'react'
import type { Action } from './api'
import { humanize, longDate, patch, post } from './api'
import { StateBadge } from './Dashboard'
import { findingHref } from './route'
import { useFetch } from './useFetch'

function ActionCard({ action, onChange }: { action: Action; onChange: () => void }) {
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({ to: action.to, subject: action.subject, body: action.body })
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      setEditing(false)
      onChange()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const url = `/api/actions/${encodeURIComponent(action.id)}`
  const blank = !form.to.trim() || !form.subject.trim() || !form.body.trim()
  const startEdit = () => {
    setForm({ to: action.to, subject: action.subject, body: action.body })
    setEditing(true)
  }

  return (
    <article className={`card state-${action.state}`}>
      <header>
        <div>
          <strong>{humanize(action.type)}</strong> · {action.vendor}{' '}
          <StateBadge state={action.state} />
          {action.edited && <span className="badge">edited</span>}
          {action.engine === 'template' && <span className="badge warn">template fallback</span>}
        </div>
        <a href={findingHref(action.finding)} className="small">
          {action.finding_title}
        </a>
      </header>

      {action.pre_step && <p className="prestep">Before sending: {action.pre_step}</p>}
      <p className="small muted">
        {action.deadline
          ? `Send by ${longDate(action.recommended_send_by)} · deadline ${longDate(action.deadline)}`
          : 'No deadline'}
        {action.type === 'billing_dispute' && action.deadline && ' (last day for us to dispute)'}
      </p>

      {editing ? (
        <div className="form">
          <label>
            To <input value={form.to} onChange={(e) => setForm({ ...form, to: e.target.value })} />
          </label>
          <label>
            Subject{' '}
            <input
              value={form.subject}
              onChange={(e) => setForm({ ...form, subject: e.target.value })}
            />
          </label>
          <textarea
            rows={16}
            value={form.body}
            onChange={(e) => setForm({ ...form, body: e.target.value })}
          />
        </div>
      ) : (
        <div className="email">
          <div>
            <span className="muted">To:</span> {action.to}
          </div>
          <div>
            <span className="muted">Subject:</span> {action.subject}
          </div>
          <pre>{action.body}</pre>
        </div>
      )}

      {action.alternative && (
        <p className="small muted">
          Alternative: a {humanize(action.alternative)} instead, if you want to keep the service.
        </p>
      )}

      <footer>
        {action.state === 'draft' &&
          (editing ? (
            <>
              <button disabled={busy || blank} onClick={() => run(() => patch(url, form))}>
                Save
              </button>
              <button disabled={busy} className="ghost" onClick={() => setEditing(false)}>
                Cancel
              </button>
            </>
          ) : (
            <>
              <button disabled={busy} className="primary" onClick={() => run(() => post(`${url}/approve`))}>
                Approve
              </button>
              <button disabled={busy} onClick={startEdit}>
                Edit
              </button>
              <button disabled={busy} className="ghost" onClick={() => run(() => post(`${url}/reject`))}>
                Reject
              </button>
            </>
          ))}
        {editing && blank && (
          <span className="error small">To, subject and body can’t be empty.</span>
        )}
        {action.state === 'rejected' && (
          <>
            <span className="muted small">Rejected</span>
            <button disabled={busy} onClick={() => run(() => post(`${url}/restore`))}>
              Undo
            </button>
          </>
        )}
        {action.state === 'approved' && (
          <button disabled={busy} className="primary" onClick={() => run(() => post(`${url}/send`))}>
            Send (simulated)
          </button>
        )}
        {action.state === 'sent' && action.sent_at && (
          <span className="muted small">Written to the outbox {new Date(action.sent_at).toLocaleString()}</span>
        )}
        {error && <span className="error">{error}</span>}
      </footer>
    </article>
  )
}

export default function ApprovalQueue({ version, onChange }: { version: number; onChange: () => void }) {
  const { data: actions, error } = useFetch<Action[]>('/api/actions', version)
  const { data: outbox } = useFetch<Action[]>('/api/outbox', version)

  if (error) return <p className="error">{error}</p>
  return (
    <>
      <h1>Approval queue</h1>
      <p className="muted">Nothing is sent without your approval.</p>
      {actions?.map((a) => <ActionCard key={a.id} action={a} onChange={onChange} />)}

      <h2>Outbox</h2>
      <p className="muted small">Simulated. Nothing is sent externally.</p>
      {outbox?.length ? (
        <ul className="outbox">
          {outbox.map((a) => (
            <li key={a.id}>
              <strong>{a.subject}</strong> → <code>{a.to}</code>{' '}
              <span className="muted small">{a.sent_at && new Date(a.sent_at).toLocaleString()}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">Empty.</p>
      )}
    </>
  )
}
