import { useState } from 'react'
import type { CorrectionOut, InvoiceFact, Term } from './api'
import { humanize, patch, post } from './api'

// The feedback loop: a reviewer corrects or rejects an extracted term or invoice.
// The correction is kept (re-extraction never overwrites it) and feeds the extraction prompt.
// Findings follow on the worker's next reconcile.

type Field = { key: string; label: string; value: unknown }

/** The text in an input back to the value's type: "" is null, numbers and yes/no as such. */
function parse(text: string, was: unknown): unknown {
  const t = text.trim()
  if (t === '') return null
  if (typeof was === 'boolean') return ['yes', 'true'].includes(t.toLowerCase())
  if (typeof was === 'number' && !Number.isNaN(Number(t))) return Number(t)
  return t
}

const show = (v: unknown) =>
  v === null || v === undefined ? '' : typeof v === 'boolean' ? (v ? 'yes' : 'no') : String(v)

function Editor({
  fields,
  rejectLabel,
  onSave,
  onReject,
  onDone,
}: {
  fields: Field[]
  rejectLabel: string
  onSave: (changed: Record<string, unknown>) => Promise<unknown>
  onReject: () => Promise<unknown>
  onDone: () => void
}) {
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [demoted, setDemoted] = useState<string | null>(null)

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      const out = (await fn()) as CorrectionOut
      setDemoted(out?.demoted ?? null)
      setOpen(false)
      onDone()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const start = () => {
    setForm(Object.fromEntries(fields.map((f) => [f.key, show(f.value)])))
    setOpen(true)
  }
  const changed = () =>
    Object.fromEntries(
      fields
        .filter((f) => form[f.key] !== show(f.value))
        .map((f) => [f.key, parse(form[f.key], f.value)]),
    )

  if (!open)
    return (
      <div className="correct">
        <button className="ghost small" disabled={busy} onClick={start}>
          Correct
        </button>
        <button className="ghost small" disabled={busy} onClick={() => run(onReject)}>
          {rejectLabel}
        </button>
        {error && <span className="error small">{error}</span>}
        {demoted && (
          <span className="small muted">
            Script {demoted} demoted: this sender goes back to the LLM until scriptgen rewrites it.
          </span>
        )}
      </div>
    )
  return (
    <div className="form correct-form">
      {fields.map((f) => (
        <label key={f.key}>
          {humanize(f.label)}
          <input
            value={form[f.key]}
            onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
          />
        </label>
      ))}
      <div className="correct">
        <button
          className="primary small"
          disabled={busy || Object.keys(changed()).length === 0}
          onClick={() => run(() => onSave(changed()))}
        >
          Save correction
        </button>
        <button className="ghost small" disabled={busy} onClick={() => setOpen(false)}>
          Cancel
        </button>
        {error && <span className="error small">{error}</span>}
      </div>
    </div>
  )
}

export function CorrectTerm({ term, onDone }: { term: Term; onDone: () => void }) {
  if (term.status === 'rejected') return null
  const url = `/api/terms/${encodeURIComponent(term.id)}`
  const fields: Field[] = [
    ...Object.entries(term.value).map(([k, v]) => ({ key: `value.${k}`, label: k, value: v })),
    { key: 'plan_label', label: 'plan label', value: term.plan_label },
    { key: 'effective_date', label: 'effective date (YYYY-MM-DD)', value: term.effective_date },
  ]
  const save = (changed: Record<string, unknown>) => {
    const body: Record<string, unknown> = {}
    const value: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(changed)) {
      if (k.startsWith('value.')) value[k.slice(6)] = v
      else body[k] = v
    }
    if (Object.keys(value).length) body.value = value
    return patch(url, body)
  }
  return (
    <Editor
      fields={fields}
      rejectLabel="Not a term"
      onSave={save}
      onReject={() => post(`${url}/reject`)}
      onDone={onDone}
    />
  )
}

export function CorrectInvoice({ fact, onDone }: { fact: InvoiceFact; onDone: () => void }) {
  const url = `/api/invoice-facts/${encodeURIComponent(fact.id)}`
  const fields: Field[] = [
    { key: 'amount', label: 'amount', value: fact.amount },
    { key: 'billing_period', label: 'billing period', value: fact.billing_period },
    { key: 'invoice_date', label: 'invoice date', value: fact.invoice_date },
    { key: 'invoice_number', label: 'invoice number', value: fact.invoice_number },
    { key: 'seats_billed', label: 'seats billed', value: fact.seats_billed },
  ]
  return (
    <Editor
      fields={fields}
      rejectLabel="Not an invoice"
      onSave={(changed) => patch(url, changed)}
      onReject={() => post(`${url}/reject`)}
      onDone={onDone}
    />
  )
}

export const CorrectedBadge = ({ corrected }: { corrected: boolean }) =>
  corrected ? <span className="badge">corrected</span> : null
