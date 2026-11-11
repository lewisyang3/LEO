"""Writing extracted items through `Repo`: shared by the seed and the worker.

Keys are `keys.term(doc_key, type, span)` and `keys.invoice_fact(doc_key, span)`, with the
span from the item's first quote (`leo.rules.span_key`) in the document's stored text, so a
rerun on the same text writes the same rows (D5). Citations are set from all quotes.

Two items of one extraction can get the same key: the same type cited by the same first quote.
Pass one `KeySet` per extraction and the later item gets a `#n` suffix, in extraction order, so
neither overwrites the other.
"""

from typing import Any
from uuid import UUID

from pydantic_core import to_jsonable_python

from leo.db import keys
from leo.db.models import ContractTerm
from leo.db.models import InvoiceFact as InvoiceFactRow
from leo.db.repo import Repo
from leo.rules import span_key
from leo.types import Contact, ExtractedTerm, InvoiceFact


def jsonb(value: Any) -> Any:
    """JSON-ready values for JSONB: the engine's json.dumps can't take Decimal, date or models."""
    return None if value is None else to_jsonable_python(value)


def vendor_uuid(vendor: str) -> UUID | None:
    """`""` is the interfaces' "no vendor" (validation failed closed): stored as NULL."""
    return UUID(vendor) if vendor else None


class KeySet:
    """The keys used so far in one extraction."""

    def __init__(self) -> None:
        self.used: set[str] = set()

    def unique(self, key: str) -> str:
        out, n = key, 1
        while out in self.used:
            n += 1
            out = f"{key}#{n}"
        self.used.add(out)
        return out


def write_term(
    repo: Repo,
    document_id: UUID,
    doc_key: str,
    text: str,
    t: ExtractedTerm,
    vendor_id: UUID | None,
    seen: KeySet | None = None,
    raw_model_output: dict | None = None,
) -> tuple[str, UUID]:
    """Upsert one term found in the document, and its citations. Returns (key, id)."""
    key = keys.term(doc_key, str(t.type), span_key(text, t.quotes[0]))
    if seen is not None:
        key = seen.unique(key)
    cols: dict[str, Any] = dict(
        vendor_id=vendor_id,
        document_id=document_id,
        type=str(t.type),
        value=jsonb(t.value),
        old_value=jsonb(t.old_value),
        plan_label=t.plan_label,
        agreement_ref=t.agreement_ref,
        effective_date=t.effective_date,
        status=str(t.status),
        notes=t.notes,
    )
    if raw_model_output is not None:
        cols["raw_model_output"] = raw_model_output
    tid = repo.upsert_term(key, **cols)
    if not repo.is_corrected(ContractTerm, tid):  # a reviewer's row keeps its citations too
        repo.set_citations("term_id", tid, document_id, text, t.quotes)
    return key, tid


def write_invoice_fact(
    repo: Repo,
    document_id: UUID,
    doc_key: str,
    text: str,
    f: InvoiceFact,
    vendor_id: UUID | None,
    seen: KeySet | None = None,
    raw_model_output: dict | None = None,
) -> tuple[str, UUID]:
    key = keys.invoice_fact(doc_key, span_key(text, f.quotes[0]))
    if seen is not None:
        key = seen.unique(key)
    cols: dict[str, Any] = dict(
        vendor_id=vendor_id,
        document_id=document_id,
        billing_period=f.billing_period,
        invoice_date=f.invoice_date,
        amount=f.amount,
        invoice_number=f.invoice_number,
        seats_billed=f.seats_billed,
        notes=f.notes,
        status=str(f.status),
    )
    if raw_model_output is not None:
        cols["raw_model_output"] = raw_model_output
    fid = repo.upsert_invoice_fact(key, **cols)
    if not repo.is_corrected(InvoiceFactRow, fid):
        repo.set_citations("invoice_fact_id", fid, document_id, text, f.quotes)
    return key, fid


def write_contact(repo: Repo, vendor_id: UUID, document_id: UUID, text: str, c: Contact) -> UUID:
    """A vendor contact, cited by its quote in the document."""
    cid = repo.upsert_contact(vendor_id, c.email, c.purpose)
    repo.set_citations("vendor_contact_id", cid, document_id, text, [c.quote])
    return cid
