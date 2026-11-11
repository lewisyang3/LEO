"""Reads from the database into the shared types.

The API, and the worker's reconcile and draft stages, read stored rows through here. Reads are
scoped to one tenant and never write. Ids are `str(uuid)`; document references are document
paths, as in `leo.types`.

The finding link tables have no ordinal, and UUIDv7s written in the same millisecond don't sort
in write order, so linked items are sorted by their own fields: terms by (type, effective date,
id), invoice facts by invoice date and transactions by date.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from leo.db import models as m
from leo.interfaces import Ledger
from leo.match.duplicates import PROBABLE_DUPLICATE
from leo.types import (
    EXCLUDED_STATUSES,
    Contact,
    ExtractedTerm,
    Finding,
    InvoiceFact,
    SourceDocument,
    Transaction,
    Vendor,
    extracted_term_adapter,
    finding_adapter,
)

CURRENT = "current"


@dataclass(frozen=True)
class StoredCitation:
    document: str  # path
    quote: str
    start: int | None
    end: int | None


@dataclass(frozen=True)
class PriceSeries:
    """One price history: a vendor's price terms for one agreement and plan."""

    agreement_ref: str | None
    plan_label: str | None
    terms: list[str]  # term ids, by effective date (undated last)


def _s(x: UUID | None) -> str | None:
    return None if x is None else str(x)


class _Reader:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.s = session
        self.t = tenant_id

    def rows(self, model: type[m.Base], *order: Any) -> list[Any]:
        return list(self.s.scalars(select(model).where(model.tenant_id == self.t).order_by(*order)))

    def doc_paths(self) -> dict[UUID, str]:
        q = select(m.SourceDocument.id, m.SourceDocument.path).where(
            m.SourceDocument.tenant_id == self.t
        )
        return dict(self.s.execute(q).all())

    def citations(self) -> dict[UUID, list[StoredCitation]]:
        """Owner id (term, invoice fact or contact) -> its citations, in ordinal order."""
        paths = self.doc_paths()
        out: dict[UUID, list[StoredCitation]] = defaultdict(list)
        for c in self.rows(m.Citation, m.Citation.ordinal):
            owner = c.term_id or c.invoice_fact_id or c.vendor_contact_id
            out[owner].append(
                StoredCitation(paths[c.document_id], c.quote, c.start_offset, c.end_offset)
            )
        return out


def load_citations(session: Session, tenant_id: UUID) -> dict[str, list[StoredCitation]]:
    return {str(k): v for k, v in _Reader(session, tenant_id).citations().items()}


def load_ledger(session: Session, tenant_id: UUID) -> Ledger:
    r = _Reader(session, tenant_id)
    paths = r.doc_paths()
    cites = r.citations()

    aliases: dict[UUID, list[str]] = defaultdict(list)
    for a in r.rows(m.VendorAlias, m.VendorAlias.alias):
        aliases[a.vendor_id].append(a.alias)
    contacts: dict[UUID, list[Contact]] = defaultdict(list)
    for c in r.rows(m.VendorContact, m.VendorContact.email, m.VendorContact.purpose):
        for cite in cites.get(c.id, [])[:1]:
            contacts[c.vendor_id].append(
                Contact(email=c.email, purpose=c.purpose, document=cite.document, quote=cite.quote)
            )
    vendors = [
        Vendor(
            id=str(v.id),
            canonical_name=v.canonical_name,
            aliases=aliases[v.id],
            contacts=contacts[v.id],
            notes=v.notes,
        )
        for v in r.rows(m.Vendor, m.Vendor.canonical_name)
    ]

    # A linked duplicate's terms and facts are left out: they count once, under the original.
    dup_of = {
        link.document_id: paths[link.linked_document_id]
        for link in r.rows(m.DocumentLink, m.DocumentLink.id)
        if link.link_type == PROBABLE_DUPLICATE
    }
    documents = [
        SourceDocument(
            path=d.path,
            doc_class=d.doc_class,
            vendor=_s(d.vendor_id),
            sha256=d.content_sha256,
            probable_duplicate_of=dup_of.get(d.id),
            notes=d.notes,
        )
        for d in r.rows(m.SourceDocument, m.SourceDocument.path)
    ]

    terms: list[ExtractedTerm] = [
        extracted_term_adapter.validate_python(
            {
                "id": str(t.id),
                "type": t.type,
                "vendor": _s(t.vendor_id) or "",
                "document": paths[t.document_id],
                "quotes": [c.quote for c in cites[t.id]],
                "value": t.value,
                "old_value": t.old_value,
                "plan_label": t.plan_label,
                "agreement_ref": t.agreement_ref,
                "effective_date": t.effective_date,
                "status": t.status,
                "superseded_by": _s(t.superseded_by),
                "supersedes": _s(t.supersedes),
                "notes": t.notes,
            }
        )
        for t in r.rows(
            m.ContractTerm, m.ContractTerm.type, m.ContractTerm.effective_date, m.ContractTerm.id
        )
        if t.document_id not in dup_of
    ]
    facts = [
        InvoiceFact(
            id=str(f.id),
            vendor=_s(f.vendor_id) or "",
            document=paths[f.document_id],
            billing_period=f.billing_period,
            invoice_date=f.invoice_date,
            amount=f.amount,
            quotes=[c.quote for c in cites[f.id]],
            invoice_number=f.invoice_number,
            seats_billed=f.seats_billed,
            matches_transaction=_s(f.matches_transaction),
            status=f.status,
            notes=f.notes,
        )
        for f in r.rows(m.InvoiceFact, m.InvoiceFact.invoice_date, m.InvoiceFact.id)
        if f.document_id not in dup_of
    ]
    txn_rows = r.rows(m.Transaction, m.Transaction.date, m.Transaction.id)
    transactions = [
        Transaction(
            id=str(t.id),
            date=t.date,
            descriptor=t.descriptor,
            amount=t.amount,
            currency=t.currency,
            card_last4=t.card_last4,
            seats_billed=t.seats_billed,
        )
        for t in txn_rows
    ]
    return Ledger(
        vendors=vendors,
        documents=documents,
        terms=terms,
        invoice_facts=facts,
        transactions=transactions,
        transaction_vendor_map={str(t.id): str(t.vendor_id) for t in txn_rows if t.vendor_id},
        transaction_plan_map={str(t.id): t.plan_label for t in txn_rows if t.plan_label},
    )


def load_findings(session: Session, tenant_id: UUID, ledger: Ledger | None = None) -> list[Finding]:
    """Current findings, rebuilt from their rows and links. Pass `ledger` to reuse one already
    loaded; it supplies the documents, order and duplicate links of the linked items."""
    r = _Reader(session, tenant_id)
    ledger = ledger or load_ledger(session, tenant_id)
    # From every stored row, not the ledger: the ledger leaves out a linked duplicate's items,
    # and a finding written before the link may still cite them.
    paths = r.doc_paths()
    term_docs = {
        str(i): paths[d]
        for i, d in session.execute(
            select(m.ContractTerm.id, m.ContractTerm.document_id).where(
                m.ContractTerm.tenant_id == tenant_id
            )
        )
    }
    fact_docs = {
        str(i): paths[d]
        for i, d in session.execute(
            select(m.InvoiceFact.id, m.InvoiceFact.document_id).where(
                m.InvoiceFact.tenant_id == tenant_id
            )
        )
    }
    dup_of = {d.path: d.probable_duplicate_of for d in ledger.documents}
    order = {
        x.id: i for i, x in enumerate([*ledger.terms, *ledger.invoice_facts, *ledger.transactions])
    }

    links_t: dict[UUID, list[tuple[str, bool]]] = defaultdict(list)
    for x in r.rows(m.FindingTerm):
        links_t[x.finding_id].append((str(x.term_id), x.is_deadline_basis))
    links_f: dict[UUID, list[tuple[str, bool]]] = defaultdict(list)
    for x in r.rows(m.FindingInvoiceFact):
        links_f[x.finding_id].append((str(x.invoice_fact_id), x.is_deadline_basis))
    links_x: dict[UUID, list[str]] = defaultdict(list)
    for x in r.rows(m.FindingTransaction):
        links_x[x.finding_id].append(str(x.transaction_id))

    def ordered(ids: list[str]) -> list[str]:
        return sorted(ids, key=lambda i: order.get(i, len(order)))

    out: list[Finding] = []
    rows = [f for f in r.rows(m.Finding, m.Finding.id) if f.status == CURRENT]
    for f in rows:
        terms = ordered([t for t, _ in links_t[f.id]])
        facts = ordered([x for x, _ in links_f[f.id]])
        basis_terms = ordered([t for t, b in links_t[f.id] if b])
        basis_fact = next((x for x in facts if (x, True) in links_f[f.id]), None)
        anchor = str(f.anchor_term_id)
        documents: list[str] = []
        for d in [term_docs[anchor], *(fact_docs[x] for x in facts)]:
            if d not in documents:
                documents.append(d)
        data: dict[str, Any] = {
            "id": str(f.id),
            "kind": f.kind,
            "vendor": str(f.vendor_id),
            "title": f.title,
            "terms": terms,
            "transactions": ordered(links_x[f.id]),
            "documents": documents,
            "invoice_facts": facts,
            "linked_duplicates": [p for d in documents for p, orig in dup_of.items() if orig == d],
            "plan_label": f.plan_label,
            "anchor": {"term": anchor, "transaction": _s(f.anchor_transaction_id)},
            "deadline": f.deadline,
            "recommended_send_by": f.recommended_send_by,
            "deadline_basis": {"terms": basis_terms, "invoice_fact": basis_fact}
            if basis_terms or basis_fact
            else None,
            "deadline_notes": f.deadline_notes,
            "impact": f.impact,
        }
        if f.computation is not None:  # F1 and F2 have no computation field
            data["computation"] = f.computation
        out.append(finding_adapter.validate_python(data))
    return out


def _duplicate_documents(tenant_id: UUID):
    """Ids of documents linked as a probable duplicate of another (the forward, not the
    original). Their facts are counted once, under the original."""
    return select(m.DocumentLink.document_id).where(
        m.DocumentLink.tenant_id == tenant_id, m.DocumentLink.link_type == PROBABLE_DUPLICATE
    )


def load_price_history(session: Session, tenant_id: UUID, vendor_id: UUID) -> list[PriceSeries]:
    """The price history: a vendor's `price` terms, one series per agreement_ref
    and plan_label, by effective date. Terms from probable-duplicate documents are left out, and so
    are `needs_review` terms, which failed validation, and `rejected` ones (the reconciler ignores
    both)."""
    q = (
        select(m.ContractTerm.id, m.ContractTerm.agreement_ref, m.ContractTerm.plan_label)
        .where(
            m.ContractTerm.tenant_id == tenant_id,
            m.ContractTerm.vendor_id == vendor_id,
            m.ContractTerm.type == "price",
            m.ContractTerm.status.not_in(EXCLUDED_STATUSES),
            m.ContractTerm.document_id.not_in(_duplicate_documents(tenant_id)),
        )
        .order_by(m.ContractTerm.effective_date.asc().nulls_last(), m.ContractTerm.id)
    )
    series: dict[tuple[str | None, str | None], list[str]] = defaultdict(list)
    for term_id, ref, plan in session.execute(q).all():
        series[(ref, plan)].append(str(term_id))
    return [
        PriceSeries(agreement_ref=ref, plan_label=plan, terms=ids)
        for (ref, plan), ids in sorted(
            series.items(), key=lambda kv: (kv[0][1] or "", kv[0][0] or "")
        )
    ]
