import { useState } from 'react'
import type { PricePoint, PriceSeries, QuoteRef, TermOut, VendorDetail as Detail } from './api'
import { formatTermValue, humanize, longDate, money } from './api'
import { DaysLeft, StateBadge } from './Dashboard'
import { CorrectedBadge, CorrectInvoice, CorrectTerm } from './Correct'
import DocumentViewer from './DocumentViewer'
import { findingHref } from './route'
import { useFetch } from './useFetch'

type Pick = (c: QuoteRef) => void
type Props = { active: QuoteRef | null; pick: Pick; reload: () => void }

function Quotes({ citations, active, pick }: { citations: QuoteRef[]; active: QuoteRef | null; pick: Pick }) {
  return citations.map((c) => (
    <button
      key={c.quote}
      className={`quote ${active?.document === c.document && active.quote === c.quote ? 'active' : ''}`}
      onClick={() => pick(c)}
    >
      “{c.quote}”
    </button>
  ))
}

/** The change from the previous price: up is a warning, down is good, none is quiet. */
function Change({ change }: { change: number | null }) {
  if (change === null) return null
  if (change === 0) return <span className="change muted">no change</span>
  return (
    <span className={`change ${change > 0 ? 'up' : 'down'}`}>
      {change > 0 ? '+' : '−'}
      {money(Math.abs(change))}
    </span>
  )
}

/** Latest price in effect is current; earlier ones superseded; later ones not yet in effect. */
function pointStatus(points: PricePoint[], i: number): 'current' | 'superseded' | 'upcoming' {
  if (!points[i].in_effect) return 'upcoming'
  const last = points.findLastIndex((p) => p.in_effect)
  return i === last ? 'current' : 'superseded'
}

const STATUS_LABEL = { current: 'in effect', superseded: 'superseded', upcoming: 'not yet in effect' }

function History({ series, active, pick, reload }: { series: PriceSeries } & Props) {
  return (
    <div className="item series">
      <div className="series-head">
        <strong>{series.plan_label ?? 'Price'}</strong>
        {series.agreement_ref && <span className="muted"> · {series.agreement_ref}</span>}
      </div>
      <ol className="timeline">
        {series.points.map(({ term: { term, citations, corrected }, change }, i) => {
          const status = pointStatus(series.points, i)
          return (
            <li key={term.id} className={`point ${status}`}>
              <div className="point-line">
                <span className="when">
                  {term.effective_date ? longDate(term.effective_date) : 'undated'}
                </span>
                <span className="amount">{formatTermValue(term.type, term.value)}</span>
                <Change change={change} />
                <span className={`badge status-${status}`}>{STATUS_LABEL[status]}</span>
                <CorrectedBadge corrected={corrected} />
              </div>
              <Quotes citations={citations} active={active} pick={pick} />
              <CorrectTerm term={term} onDone={reload} />
            </li>
          )
        })}
      </ol>
    </div>
  )
}

function Term({ t: { term, citations, corrected }, active, pick, reload }: { t: TermOut } & Props) {
  return (
    <div className="item">
      <div>
        <strong>{humanize(term.type)}</strong>
        {term.plan_label && <span className="muted"> · {term.plan_label}</span>}
        {term.effective_date && (
          <span className="muted"> · effective {longDate(term.effective_date)}</span>
        )}
        {term.status !== 'current' && <span className="badge">{term.status}</span>}
        <CorrectedBadge corrected={corrected} />
      </div>
      <div className="small muted">{formatTermValue(term.type, term.value)}</div>
      <Quotes citations={citations} active={active} pick={pick} />
      <CorrectTerm term={term} onDone={reload} />
    </div>
  )
}

export default function VendorDetail({ id, version }: { id: string; version: number }) {
  const { data: d, error, reload } = useFetch<Detail>(`/api/vendors/${encodeURIComponent(id)}`, version)
  const [citation, setCitation] = useState<QuoteRef | null>(null)

  if (error) return <p className="error">{error}</p>
  if (!d) return <p className="muted">Loading…</p>

  const findings = new Map(d.findings.map((f) => [f.id, f]))
  const atStake = d.findings.reduce((n, f) => n + f.at_stake, 0)
  const spend = d.transactions.reduce((n, t) => n + t.transaction.amount, 0)
  const otherTerms = d.terms.filter((t) => t.term.type !== 'price')
  const props = { active: citation, pick: setCitation, reload }

  return (
    <>
      <p>
        <a href="#/vendors">← All vendors</a>
      </p>
      <h1>{d.vendor.canonical_name}</h1>
      {d.vendor.aliases.length > 0 && (
        <p className="muted">Also seen as {d.vendor.aliases.join(', ')}</p>
      )}

      <section className="totals">
        <div>
          <strong>{d.findings.length}</strong>
          <span>open findings</span>
        </div>
        <div>
          <strong>{money(atStake)}</strong>
          <span>at stake</span>
        </div>
        <div>
          <strong>{money(spend)}</strong>
          <span>
            card spend · {d.transactions.length} {d.transactions.length === 1 ? 'charge' : 'charges'}
          </span>
        </div>
      </section>

      <div className="split">
        <div className="left">
          <section>
            <h2>Findings</h2>
            {d.findings.length === 0 ? (
              <p className="muted">No open findings for this vendor.</p>
            ) : (
              <table className="rows">
                <tbody>
                  {d.findings.map((f) => (
                    <tr key={f.id} onClick={() => (window.location.hash = findingHref(f.id))}>
                      <td>
                        <DaysLeft days={f.days_left} />
                      </td>
                      <td>
                        <a href={findingHref(f.id)}>{f.title}</a>
                        <div className="muted small">{humanize(f.kind)}</div>
                      </td>
                      <td className="num">{money(f.at_stake)}</td>
                      <td>
                        <StateBadge state={f.action_state} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {d.price_history.length > 0 && (
            <section>
              <h2>Price history</h2>
              <p className="muted small">
                One history per agreement and plan, oldest first. Click a quote to see its source.
              </p>
              {d.price_history.map((s) => (
                <History key={`${s.agreement_ref}|${s.plan_label}`} series={s} {...props} />
              ))}
            </section>
          )}

          <section>
            <h2>Contract terms</h2>
            {otherTerms.length === 0 ? (
              <p className="muted">
                {d.terms.length === 0 ? 'No contract on file.' : 'Only prices, shown above.'}
              </p>
            ) : (
              otherTerms.map((t) => <Term key={t.term.id} t={t} {...props} />)
            )}
          </section>

          <section>
            <h2>Card charges</h2>
            {d.transactions.length === 0 ? (
              <p className="muted">No card charges matched to this vendor.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Charge</th>
                    <th className="num">Amount</th>
                    <th>Finding</th>
                  </tr>
                </thead>
                <tbody>
                  {d.transactions.map(({ transaction: t, plan_label, invoice_fact, invoice_corrected, findings: ids }) => (
                    <tr key={t.id}>
                      <td className="nowrap">{longDate(t.date)}</td>
                      <td>
                        <code className="nowrap">{t.descriptor}</code>
                        {(plan_label || t.seats_billed !== null) && (
                          <div className="muted small">
                            {[plan_label, t.seats_billed !== null && `${t.seats_billed} seats`]
                              .filter(Boolean)
                              .join(' · ')}
                          </div>
                        )}
                        {invoice_fact && (
                          <div className="small">
                            Invoice <span className="nowrap">{invoice_fact.invoice_number ?? invoice_fact.billing_period}</span>
                            <CorrectedBadge corrected={invoice_corrected} />
                            <CorrectInvoice fact={invoice_fact} onDone={reload} />
                          </div>
                        )}
                      </td>
                      <td className="num">{money(t.amount)}</td>
                      <td>
                        {ids.map((fid) => (
                          <a key={fid} href={findingHref(fid)} className="block">
                            {humanize(findings.get(fid)?.kind ?? 'finding')}
                          </a>
                        ))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {d.linked_duplicates.length > 0 && (
              <p className="muted small">
                Duplicates linked, counted once: {d.linked_duplicates.join(', ')}
              </p>
            )}
          </section>
        </div>

        <aside className="right">
          <DocumentViewer citation={citation} hint="Click any quote to see where it came from." />
        </aside>
      </div>
    </>
  )
}
