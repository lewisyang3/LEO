import { useMemo, useState } from 'react'
import type { FindingDetail as Detail, NumberRef, QuoteRef } from './api'
import { formatNumber, formatTermValue, humanize, longDate, money } from './api'
import { DaysLeft, StateBadge } from './Dashboard'
import DocumentViewer from './DocumentViewer'
import { vendorHref } from './route'
import { useFetch } from './useFetch'

export default function FindingDetail({ id, version }: { id: string; version: number }) {
  const { data: d, error } = useFetch<Detail>(`/api/findings/${encodeURIComponent(id)}`, version)
  const [number, setNumber] = useState<NumberRef | null>(null)
  const [citation, setCitation] = useState<QuoteRef | null>(null)

  // item id -> its citations and a short label, for the source chips
  const items = useMemo(() => {
    const m = new Map<string, { label: string; citations: QuoteRef[] }>()
    d?.terms.forEach((t) =>
      m.set(t.term.id, { label: humanize(t.term.type), citations: t.citations }),
    )
    d?.invoice_facts.forEach((x) =>
      m.set(x.fact.id, {
        label: `invoice ${x.fact.invoice_number ?? x.fact.billing_period}`,
        citations: x.citations,
      }),
    )
    return m
  }, [d])

  if (error) return <p className="error">{error}</p>
  if (!d) return <p className="muted">Loading…</p>

  const pickNumber = (n: NumberRef) => {
    setNumber(n)
    setCitation(items.get(n.sources[0])?.citations[0] ?? null)
  }
  const pickQuote = (c: QuoteRef) => {
    setNumber(null)
    setCitation(c)
  }
  const isActive = (c: QuoteRef) =>
    citation !== null && c.document === citation.document && c.quote === citation.quote

  const s = d.summary
  const headline = d.numbers.find((n) => n.headline)
  const rest = d.numbers.filter((n) => !n.headline)
  const groups: [string, NumberRef[]][] = [
    ['Deadline', rest.filter((n) => !n.key.includes('.'))],
    ['Impact', rest.filter((n) => n.key.startsWith('impact.'))],
    ['How it’s calculated', rest.filter((n) => n.key.startsWith('computation.'))],
  ]
  return (
    <>
      <p>
        <a href="#/">← All findings</a>
      </p>
      <h1>{s.title}</h1>
      <p className="muted">
        <a href={vendorHref(d.vendor.id)}>{d.vendor.canonical_name}</a> · {humanize(s.kind)}
        {s.plan_label && ` · ${s.plan_label}`} · <DaysLeft days={s.days_left} long />
        {s.deadline && ` · deadline ${longDate(s.deadline)}`}
      </p>

      <div className="split">
        <div className="left">
          <section>
            <h2>Numbers</h2>
            <p className="muted small">Click a number to see its source.</p>
            {headline && (
              <button
                className={`number headline ${number?.key === headline.key ? 'active' : ''}`}
                onClick={() => pickNumber(headline)}
              >
                <span className="value">{formatNumber(headline)}</span>
                <span className="label">at stake · {headline.label.toLowerCase()}</span>
              </button>
            )}
            {groups.map(
              ([title, list]) =>
                list.length > 0 && (
                  <div key={title}>
                    <h3>{title}</h3>
                    <div className="numbers">
                      {list.map((n) => (
                        <button
                          key={n.key}
                          className={`number ${number?.key === n.key ? 'active' : ''}`}
                          onClick={() => pickNumber(n)}
                        >
                          <span className="label">{n.label}</span>
                          <span className="value">{formatNumber(n)}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                ),
            )}
            {number && number.sources.length > 1 && (
              <div className="chips">
                From:{' '}
                {number.sources.map((src) => {
                  const it = items.get(src)
                  const c = it?.citations[0]
                  return (
                    <button
                      key={src}
                      className={`chip ${c && isActive(c) ? 'active' : ''}`}
                      onClick={() => c && setCitation(c)}
                    >
                      {it?.label ?? src}
                    </button>
                  )
                })}
              </div>
            )}
            {d.finding.deadline_notes && <p className="notes">{d.finding.deadline_notes}</p>}
          </section>

          <section>
            <h2>Contract terms</h2>
            {d.terms.map(({ term, citations }) => (
              <div key={term.id} className="item">
                <div>
                  <strong>{humanize(term.type)}</strong>
                  {term.plan_label && <span className="muted"> · {term.plan_label}</span>}
                  {term.effective_date && (
                    <span className="muted"> · effective {longDate(term.effective_date)}</span>
                  )}
                  {term.status !== 'current' && <span className="badge">{term.status}</span>}
                </div>
                <div className="small muted">{formatTermValue(term.type, term.value)}</div>
                {citations.map((c) => (
                  <button
                    key={c.quote}
                    className={`quote ${isActive(c) ? 'active' : ''}`}
                    onClick={() => pickQuote(c)}
                  >
                    “{c.quote}”
                  </button>
                ))}
              </div>
            ))}
          </section>

          {d.invoice_facts.length > 0 && (
            <section>
              <h2>Invoices</h2>
              {d.invoice_facts.map(({ fact, citations }) => (
                <div key={fact.id} className="item">
                  <div>
                    <strong>{fact.invoice_number ?? fact.id}</strong>{' '}
                    <span className="muted">
                      · {fact.billing_period} · dated {longDate(fact.invoice_date)} ·{' '}
                      {money(fact.amount)}
                      {fact.seats_billed !== null && ` · ${fact.seats_billed} seats`}
                    </span>
                  </div>
                  {citations.map((c) => (
                    <button
                      key={c.quote}
                      className={`quote ${isActive(c) ? 'active' : ''}`}
                      onClick={() => pickQuote(c)}
                    >
                      “{c.quote}”
                    </button>
                  ))}
                </div>
              ))}
            </section>
          )}

          <section>
            <h2>Card transactions</h2>
            <table>
              <tbody>
                {d.transactions.map((t) => (
                  <tr key={t.id}>
                    <td>{longDate(t.date)}</td>
                    <td>
                      <code>{t.descriptor}</code>
                    </td>
                    <td className="num">{money(t.amount)}</td>
                    <td className="muted">{t.seats_billed !== null && `${t.seats_billed} seats`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {d.linked_duplicates.length > 0 && (
              <p className="muted small">
                Duplicates linked, not counted twice: {d.linked_duplicates.join(', ')}
              </p>
            )}
          </section>

          <section className="action-card">
            <h2>Drafted fix</h2>
            <p>
              <strong>{humanize(d.action.type)}</strong> to <code>{d.action.to}</code>{' '}
              <StateBadge state={d.action.state} />
            </p>
            <p className="muted">{d.action.subject}</p>
            <a href="#/actions">Review in the approval queue →</a>
          </section>
        </div>

        <aside className="right">
          <DocumentViewer citation={citation} />
        </aside>
      </div>
    </>
  )
}
