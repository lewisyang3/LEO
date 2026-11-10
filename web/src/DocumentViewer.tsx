import { useEffect, useRef } from 'react'
import type { DocumentOut, QuoteRef } from './api'
import { useFetch } from './useFetch'

/** The source document, with the cited quote highlighted and scrolled into view. */
export default function DocumentViewer({ citation }: { citation: QuoteRef | null }) {
  const path = citation?.document ?? null
  const { data: doc, error } = useFetch<DocumentOut>(
    path && `/api/documents?path=${encodeURIComponent(path)}`,
  )
  const mark = useRef<HTMLElement>(null)

  useEffect(() => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    mark.current?.scrollIntoView({ block: 'center', behavior: reduce ? 'auto' : 'smooth' })
  }, [doc, citation])

  if (!citation) {
    return <p className="muted hint">Click any number or quote to see where it came from.</p>
  }
  if (error) return <p className="error">{error}</p>
  if (!doc) return <p className="muted">Loading…</p>

  // Keep the previous document, dimmed, until the new one arrives: no loading flash.
  const stale = doc.path !== citation.document
  const { start, end } = citation
  const located = !stale && start !== null && end !== null
  return (
    <>
      <div className="doc-head">
        <code>{doc.path}</code>
        {doc.doc_class && <span className="badge">{doc.doc_class.replaceAll('_', ' ')}</span>}
      </div>
      {!stale && !located && (
        <p className="error">Quote not found in source: “{citation.quote}”</p>
      )}
      <pre className={`doc ${stale ? 'stale' : ''}`}>
        {located ? (
          <>
            {doc.text.slice(0, start)}
            <mark key={`${doc.path}:${start}`} ref={mark}>
              {doc.text.slice(start, end)}
            </mark>
            {doc.text.slice(end)}
          </>
        ) : (
          doc.text
        )}
      </pre>
    </>
  )
}
