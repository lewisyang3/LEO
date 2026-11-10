"""What the worker stores at each stage (ROADMAP 2.1), always through `Repo`, in the same
transaction that marks the job done.

- validate: the document's vendor, and its terms, invoice facts and contacts
- reconcile: supersessions, findings with their links, and a draft per new (finding, type)

Findings are keyed `keys.finding(anchor term key, anchor txn key, kind)` from the stored
natural keys, not from the ledger's UUID ids, so a wiped database rebuilds the same keys. The
drafter gets each finding with that key as its id, so the draft cache survives `make reset`.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select

from leo.actions.draft import draft_new
from leo.db import keys
from leo.db.models import ContractTerm, Transaction
from leo.db.models import SourceDocument as DocumentRow
from leo.db.repo import Repo
from leo.db.write import (
    KeySet,
    jsonb,
    vendor_uuid,
    write_contact,
    write_invoice_fact,
    write_term,
)
from leo.interfaces import Drafter, Ledger, Reconciliation, Validated
from leo.types import Finding


def store_validated(
    repo: Repo, row: DocumentRow, text: str, v: Validated, vendor_id: UUID | None
) -> None:
    """The document's vendor and every item, with its validated status. `vendor_id` is None
    when validation failed closed: the items then carry vendor `""` and are stored with no
    vendor, and no contact is stored, because a contact belongs to a vendor."""
    x = v.extraction
    if vendor_id is not None:
        repo.update_document(row.id, vendor_id=vendor_id)
    seen = KeySet()
    for t in x.terms:
        write_term(repo, row.id, row.natural_key, text, t, vendor_uuid(t.vendor), seen)
    for f in x.invoice_facts:
        write_invoice_fact(repo, row.id, row.natural_key, text, f, vendor_uuid(f.vendor), seen)
    if vendor_id is not None:
        for c in x.contacts:
            write_contact(repo, vendor_id, row.id, text, c)


def _natural_keys(repo: Repo) -> tuple[dict[str, str], dict[str, str]]:
    """Ledger id (str UUID) -> natural key, for terms and transactions."""
    s, t = repo.session, repo.tenant_id
    terms = {
        str(i): nk
        for i, nk in s.execute(
            select(ContractTerm.id, ContractTerm.natural_key).where(ContractTerm.tenant_id == t)
        )
    }
    txns = {
        str(i): nk
        for i, nk in s.execute(
            select(Transaction.id, Transaction.natural_key).where(Transaction.tenant_id == t)
        )
    }
    return terms, txns


def store_reconciliation(repo: Repo, rec: Reconciliation) -> tuple[list[Finding], dict[str, UUID]]:
    """Write supersessions and findings. Returns the findings with their natural keys as ids,
    and natural key -> finding row id. Current findings the reconciler no longer produces are
    set to `superseded`."""
    repo.reset_supersessions()
    for sup in rec.supersessions:
        repo.mark_superseded(UUID(sup.old), UUID(sup.new))

    term_nk, txn_nk = _natural_keys(repo)
    out: list[Finding] = []
    ids: dict[str, UUID] = {}
    for f in rec.findings:
        anchor_txn = f.anchor.transaction
        nk = keys.finding(
            term_nk[f.anchor.term], txn_nk[anchor_txn] if anchor_txn else None, str(f.kind)
        )
        fid = repo.upsert_finding(
            nk,
            vendor_id=UUID(f.vendor),
            kind=str(f.kind),
            title=f.title,
            plan_label=f.plan_label,
            computation=jsonb(getattr(f, "computation", None)),
            anchor_term_id=UUID(f.anchor.term),
            anchor_transaction_id=UUID(anchor_txn) if anchor_txn else None,
            deadline=f.deadline,
            recommended_send_by=f.recommended_send_by,
            deadline_notes=f.deadline_notes,
            impact=jsonb(f.impact),
            status="current",  # a finding that comes back after being superseded shows again
        )
        basis = f.deadline_basis
        repo.set_finding_links(
            fid,
            terms={UUID(t): basis is not None and t in basis.terms for t in f.terms},
            transactions=[UUID(x) for x in f.transactions],
            invoice_facts={
                UUID(x): basis is not None and basis.invoice_fact == x for x in f.invoice_facts
            },
        )
        ids[nk] = fid
        out.append(f.model_copy(update={"id": nk}))
    repo.supersede_findings(set(ids.values()))
    return out, ids


def _raw(engine: str, record: dict[str, Any] | None) -> dict[str, Any]:
    """The action's `raw_model_output`. An LLM draft that fell back to the template records
    `engine: template`, as the approval queue expects (C2)."""
    if not record:
        return {"engine": engine}
    return {**record, "engine": "template" if record.get("fallback") else engine}


def store_drafts(
    repo: Repo,
    drafter: Drafter,
    engine: str,
    findings: Sequence[Finding],
    ids: dict[str, UUID],
    ledger: Ledger,
) -> list[dict[str, Any]]:
    """Draft each (finding, type) unless the user has decided or edited its action. An
    unedited draft is redrafted, so it carries the finding's current figures; when nothing
    changed, the draft cache answers and the row is rewritten with the same content. Returns
    the drafts written, as JSON."""
    drafts = draft_new(drafter, findings, ledger, repo.action_pairs())
    records = getattr(drafter, "records", {})
    for d in drafts:
        repo.upsert_action(
            keys.action(d.finding, str(d.type)),
            finding_id=ids[d.finding],
            action_type=str(d.type),
            recipient=d.to,
            subject=d.subject,
            body=d.body,
            pre_step=d.pre_step,
            alternative=str(d.alternative) if d.alternative else None,
            raw_model_output=jsonb(_raw(engine, records.pop(d.finding, None))),
        )
    return [d.model_dump(mode="json") for d in drafts]
