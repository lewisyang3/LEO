// Types mirror leo/api/schemas.py. String-literal unions, not enums (erasableSyntaxOnly).

export type ActionState = 'draft' | 'approved' | 'rejected' | 'sent'
export type ActionType =
  | 'cancellation_notice'
  | 'renegotiation_request'
  | 'negotiation_email'
  | 'billing_dispute'
export type FindingKind =
  | 'auto_renewal_deadline'
  | 'price_increase_notice'
  | 'price_cap_exceeded'
  | 'seats_over_commitment'
  | 'escalator_applied_early'

export type Dashboard = {
  today: string
  open_findings: number
  at_stake: number
  pending_actions: number
}

export type FindingSummary = {
  id: string
  kind: FindingKind
  vendor: string
  vendor_name: string
  title: string
  plan_label: string | null
  deadline: string | null
  recommended_send_by: string | null
  days_left: number | null
  at_stake: number
  action_id: string
  action_state: ActionState
}

export type QuoteRef = { document: string; quote: string; start: number | null; end: number | null }

export type NumberRef = {
  key: string
  label: string
  value: number | string
  unit: 'usd' | 'pct' | 'count' | 'date'
  sources: string[]
  headline: boolean
}

export type Term = {
  id: string
  type: string
  document: string
  value: Record<string, unknown>
  plan_label: string | null
  effective_date: string | null
  status: string
}

export type InvoiceFact = {
  id: string
  document: string
  billing_period: string
  invoice_date: string
  amount: number
  invoice_number: string | null
  seats_billed: number | null
}

export type Transaction = {
  id: string
  date: string
  descriptor: string
  amount: number
  currency: string
  seats_billed: number | null
}

export type SourceDocument = { path: string; class: string | null; vendor: string | null }

export type Action = {
  id: string
  finding: string
  finding_title: string
  vendor: string
  type: ActionType
  to: string
  subject: string
  body: string
  pre_step: string | null
  alternative: ActionType | null
  state: ActionState
  engine: string
  edited: boolean
  sent_at: string | null
  deadline: string | null
  recommended_send_by: string | null
}

export type FindingDetail = {
  summary: FindingSummary
  finding: {
    id: string
    kind: FindingKind
    title: string
    deadline_notes: string | null
    terms: string[]
    invoice_facts: string[]
    impact: Record<string, unknown>
    computation?: Record<string, unknown>
  }
  vendor: { id: string; canonical_name: string }
  numbers: NumberRef[]
  terms: { term: Term; citations: QuoteRef[] }[]
  invoice_facts: { fact: InvoiceFact; citations: QuoteRef[] }[]
  transactions: Transaction[]
  documents: SourceDocument[]
  linked_duplicates: string[]
  action: Action
}

export type DocumentOut = { path: string; doc_class: string | null; vendor: string | null; text: string }

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!r.ok) {
    const detail = await r.json().then((j) => j.detail, () => null)
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${r.status}`)
  }
  return r.json() as Promise<T>
}

export type UploadOut = { document_id: string; path: string; created: boolean; attachments: string[] }

/** Upload one file (multipart). The browser sets the Content-Type boundary itself. */
export async function upload(file: File): Promise<UploadOut> {
  const form = new FormData()
  form.append('file', file)
  const r = await fetch('/api/documents', { method: 'POST', body: form })
  if (!r.ok) {
    const detail = await r.json().then((j) => j.detail, () => null)
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${r.status}`)
  }
  return r.json() as Promise<UploadOut>
}

export const get = <T,>(url: string) => request<T>('GET', url)
export const post = <T,>(url: string, body?: unknown) => request<T>('POST', url, body)
export const patch = <T,>(url: string, body: unknown) => request<T>('PATCH', url, body)

// --- Formatting -----------------------------------------------------------------------------

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })

export const money = (x: number) => usd.format(x)

export function longDate(iso: string | null): string {
  if (!iso) return '—'
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(y, m - 1, d).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

export function formatNumber(n: NumberRef): string {
  switch (n.unit) {
    case 'usd':
      return money(Number(n.value))
    case 'pct':
      return `${n.value}%`
    case 'date':
      return longDate(String(n.value))
    default:
      return String(n.value)
  }
}

export const humanize = (s: string) => s.replaceAll('_', ' ')

const MONEY_KEYS = new Set(['amount', 'overage_per_unit_month'])
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
const PER: Record<string, string> = { year: 'year', month: 'month', seat_month: 'seat / month' }

/** A term's value in plain words: "$1,200.00 / month", "max increase: 5%", "applies to: renewal term". */
export function formatTermValue(type: string, value: Record<string, unknown>): string {
  if (type === 'price' && typeof value.amount === 'number') {
    const per = typeof value.per === 'string' ? ` / ${PER[value.per] ?? humanize(value.per)}` : ''
    return `${money(value.amount)}${per}`
  }
  return Object.entries(value)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => {
      const label = humanize(k.replace(/_?pct$/, '')).trim() || 'percent'
      let text: string
      if (typeof v === 'boolean') text = v ? 'yes' : 'no'
      else if (typeof v === 'number' && MONEY_KEYS.has(k)) text = money(v)
      else if (typeof v === 'number' && k.endsWith('pct')) text = `${v}%`
      else if (typeof v === 'string' && ISO_DATE.test(v)) text = longDate(v)
      else text = humanize(String(v))
      return `${label}: ${text}`
    })
    .join(' · ')
}
