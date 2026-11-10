"""The repository: every write is an upsert (ROADMAP A2).

Each write is `INSERT ... ON CONFLICT DO UPDATE ... RETURNING id` on the row's natural key or
unique constraint, so a rerun returns the existing id and never creates a duplicate. Ids are
never updated. On conflict, each table's columns fall in three groups:

- identity and provenance (`insert_only`): the first writer wins. D1's re-synced copy keeps
  the original's path.
- derived values (everything else passed in): overwritten from the new write.
- user-owned (`action.state`, `approver`, `decided_at`, `edited_at`, `sent_at`): never
  written by an upsert. An action's draft content only updates while it is still an unedited
  draft, so approvals and edits survive reruns.

The caller owns the transaction; `Repo` never commits.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, delete, func, select, text, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leo.db import keys
from leo.db.models import (
    Action,
    Base,
    Citation,
    ConnectedAccount,
    ContractTerm,
    DocumentLink,
    ExtractorScript,
    Finding,
    FindingInvoiceFact,
    FindingTerm,
    FindingTransaction,
    InvoiceFact,
    Job,
    OAuthCredential,
    SourceDocument,
    Tenant,
    Transaction,
    Vendor,
    VendorAlias,
    VendorContact,
    new_uuid7,
)
from leo.rules import locate_quote
from leo.types import ActionState, TermStatus

NK = ("tenant_id", "natural_key")
DOCUMENT_INSERT_ONLY = (
    "source_kind",
    "path",
    "storage_path",
    "filename",
    "content_sha256",
    "received_at",
    "message_id",
    "thread_id",
    "sender",
    "recipient",
    "subject",
    "parent_document_id",  # an attachment's first carrier; later carriers get a document_link
)
ACTION_USER_OWNED = ("state", "approver", "decided_at", "edited_at", "sent_at")
CITATION_OWNERS = ("term_id", "invoice_fact_id", "vendor_contact_id")


class AliasConflict(ValueError):
    """The alias already names a different vendor. Merging vendors is an explicit step."""


def _upsert(
    session: Session,
    model: type[Base],
    key: Sequence[str],
    values: Mapping[str, Any],
    *,
    insert_only: Iterable[str] = (),
    where: ColumnElement[bool] | None = None,
    index_where: ColumnElement[bool] | None = None,
) -> UUID:
    """Insert or update one row on the unique index over `key`; return its id.

    Columns in `key`, `id`, `tenant_id` and `insert_only` are never updated. With nothing
    left to update, or when `where` excludes the existing row, the existing id is selected.
    """
    values = dict(values)
    values.setdefault("id", new_uuid7())
    frozen = {*key, *insert_only, "id", "tenant_id"}
    ins = insert(model).values(**values)
    target: dict[str, Any] = {"index_elements": list(key), "index_where": index_where}
    updates = {c: ins.excluded[c] for c in values if c not in frozen}
    stmt = (
        ins.on_conflict_do_update(**target, set_=updates, where=where)
        if updates
        else ins.on_conflict_do_nothing(**target)
    )
    row_id = session.scalar(stmt.returning(model.id))
    table = model.__table__
    if row_id is None:
        row_id = session.scalar(select(model.id).where(*(table.c[c] == values[c] for c in key)))
    if row_id is None:  # pragma: no cover - the conflicting row must exist
        raise RuntimeError(f"upsert into {table.name} returned no row")
    return row_id


def upsert_tenant(session: Session, slug: str, name: str) -> UUID:
    """A tenant upserts on its slug. Core inserts skip `Tenant.__init__`, so set both ids."""
    tid = new_uuid7()
    return _upsert(
        session, Tenant, ("slug",), {"id": tid, "tenant_id": tid, "slug": slug, "name": name}
    )


class Repo:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def _put(
        self, model: type[Base], key: Sequence[str], values: Mapping[str, Any], **kw: Any
    ) -> UUID:
        return _upsert(self.session, model, key, {"tenant_id": self.tenant_id, **values}, **kw)

    # --- Connected accounts (A8) -------------------------------------------------------------

    def upsert_account(self, provider: str, email_address: str) -> UUID:
        """Upserts on the address, so a reconnect keeps the id (and every document key)."""
        values = {"provider": provider, "email_address": email_address}
        return self._put(ConnectedAccount, ("tenant_id", "provider", "email_address"), values)

    def set_credential(self, account_id: UUID, ciphertext: bytes, scopes: str) -> UUID:
        """Store an account's token, already encrypted (`leo.ingest.secrets`)."""
        values = {"account_id": account_id, "ciphertext": ciphertext, "scopes": scopes}
        return self._put(OAuthCredential, ("tenant_id", "account_id"), values)

    def set_cursor(self, account_id: UUID, history_id: str, synced_at: Any) -> None:
        self.session.execute(
            update(ConnectedAccount)
            .where(ConnectedAccount.id == account_id, ConnectedAccount.tenant_id == self.tenant_id)
            .values(history_id=history_id, last_synced_at=synced_at)
        )

    # --- Vendors -----------------------------------------------------------------------------

    def upsert_vendor(self, canonical_name: str, notes: str | None = None) -> UUID:
        """`notes=None` leaves stored notes alone."""
        values = {
            "canonical_name": canonical_name,
            "normalized_name": keys.normalize_name(canonical_name),
        }
        if notes is not None:
            values["notes"] = notes
        return self._put(Vendor, ("tenant_id", "normalized_name"), values)

    def add_alias(self, vendor_id: UUID, alias: str) -> UUID:
        values = {
            "vendor_id": vendor_id,
            "alias": alias,
            "normalized_alias": keys.normalize_name(alias),
        }
        key = ("tenant_id", "normalized_alias")
        alias_id = self._put(VendorAlias, key, values, insert_only=("vendor_id", "alias"))
        owner = self.session.scalar(select(VendorAlias.vendor_id).where(VendorAlias.id == alias_id))
        if owner != vendor_id:
            raise AliasConflict(f"alias {alias!r} already belongs to vendor {owner}")
        return alias_id

    def upsert_contact(self, vendor_id: UUID, email: str, purpose: str) -> UUID:
        values = {"vendor_id": vendor_id, "email": email, "purpose": purpose}
        return self._put(VendorContact, ("tenant_id", "vendor_id", "email", "purpose"), values)

    # --- Rows with natural keys --------------------------------------------------------------

    def upsert_document(self, natural_key: str, **cols: Any) -> UUID:
        values = {"natural_key": natural_key, **cols}
        return self._put(SourceDocument, NK, values, insert_only=DOCUMENT_INSERT_ONLY)

    def update_document(self, document_id: UUID, **cols: Any) -> None:
        """Set derived columns (`doc_class`, `vendor_id`, `notes`, ...) on an ingested
        document. Identity and provenance columns are never rewritten."""
        if frozen := set(cols) & {*DOCUMENT_INSERT_ONLY, "id", "tenant_id", "natural_key"}:
            raise ValueError(f"update_document never writes provenance columns: {sorted(frozen)}")
        self.session.execute(
            update(SourceDocument)
            .where(SourceDocument.id == document_id, SourceDocument.tenant_id == self.tenant_id)
            .values(**cols)
        )

    def upsert_transaction(self, natural_key: str, **cols: Any) -> UUID:
        return self._put(Transaction, NK, {"natural_key": natural_key, **cols})

    def upsert_term(self, natural_key: str, **cols: Any) -> UUID:
        """A term a reviewer corrected or rejected (`corrected_at`) is never overwritten."""
        values = {"natural_key": natural_key, **cols}
        return self._put(ContractTerm, NK, values, where=ContractTerm.corrected_at.is_(None))

    def upsert_invoice_fact(self, natural_key: str, **cols: Any) -> UUID:
        """A fact a reviewer corrected or rejected (`corrected_at`) is never overwritten."""
        values = {"natural_key": natural_key, **cols}
        return self._put(InvoiceFact, NK, values, where=InvoiceFact.corrected_at.is_(None))

    def is_corrected(self, model: type[ContractTerm] | type[InvoiceFact], row_id: UUID) -> bool:
        """Whether a reviewer has corrected or rejected this term or invoice fact."""
        q = select(model.corrected_at).where(model.id == row_id, model.tenant_id == self.tenant_id)
        return self.session.scalar(q) is not None

    def upsert_finding(self, natural_key: str, **cols: Any) -> UUID:
        return self._put(Finding, NK, {"natural_key": natural_key, **cols})

    def upsert_action(self, natural_key: str, **cols: Any) -> UUID:
        """Draft content updates only while the action is an unedited draft: state, decisions
        and the user's edits are the user's."""
        if owned := set(cols) & set(ACTION_USER_OWNED):
            raise ValueError(f"upsert_action never writes user-owned columns: {sorted(owned)}")
        values = {"natural_key": natural_key, **cols}
        return self._put(
            Action,
            NK,
            values,
            where=(Action.state == ActionState.draft) & Action.edited_at.is_(None),
        )

    def upsert_job(self, natural_key: str, **cols: Any) -> UUID:
        """Sets updated_at itself: ON CONFLICT DO UPDATE skips the ORM's onupdate."""
        return self._put(Job, NK, {"natural_key": natural_key, **cols, "updated_at": func.now()})

    def set_supersession(
        self, term_id: UUID, superseded_by: UUID | None, supersedes: UUID | None
    ) -> None:
        """A second pass after all terms exist: supersession links point both ways."""
        self.session.execute(
            update(ContractTerm)
            .where(ContractTerm.id == term_id, ContractTerm.tenant_id == self.tenant_id)
            .values(superseded_by=superseded_by, supersedes=supersedes)
        )

    def reset_supersessions(self) -> None:
        """Clear every supersession link, and set `superseded` terms back to `current`. The
        reconcile stage (2.1) recomputes supersessions whole, then `mark_superseded`s them;
        `needs_review` is never touched."""
        t = ContractTerm
        self.session.execute(
            update(t)
            .where(t.tenant_id == self.tenant_id, t.status == TermStatus.superseded)
            .values(status=TermStatus.current)
        )
        self.session.execute(
            update(t)
            .where(
                t.tenant_id == self.tenant_id,
                (t.superseded_by.is_not(None)) | (t.supersedes.is_not(None)),
            )
            .values(superseded_by=None, supersedes=None)
        )

    def mark_superseded(self, old_id: UUID, new_id: UUID) -> None:
        """`old_id` is superseded by `new_id`: both links, and the old term's status."""
        t = ContractTerm
        self.session.execute(
            update(t)
            .where(t.id == old_id, t.tenant_id == self.tenant_id)
            .values(superseded_by=new_id, status=TermStatus.superseded)
        )
        self.session.execute(
            update(t).where(t.id == new_id, t.tenant_id == self.tenant_id).values(supersedes=old_id)
        )

    def supersede_findings(self, keep: set[UUID]) -> None:
        """Set this tenant's `current` findings other than `keep` to `superseded`: the
        reconciler no longer produces them. Their actions stay, with the user's decisions."""
        self.session.execute(
            update(Finding)
            .where(
                Finding.tenant_id == self.tenant_id,
                Finding.status == "current",
                Finding.id.not_in(keep) if keep else true(),
            )
            .values(status="superseded")
        )

    def action_pairs(self) -> set[tuple[str, str]]:
        """(finding natural key, action type) for every action that must not be redrafted: one
        the user decided (approved, sent, rejected) or edited. An unedited draft is redrafted on
        each reconcile, so it follows its finding's current figures (2.1 review)."""
        rows = self.session.execute(
            select(Finding.natural_key, Action.action_type)
            .join(
                Finding, (Finding.id == Action.finding_id) & (Finding.tenant_id == Action.tenant_id)
            )
            .where(
                Action.tenant_id == self.tenant_id,
                (Action.state != ActionState.draft) | Action.edited_at.is_not(None),
            )
        )
        return {(nk, t) for nk, t in rows}

    # --- Citations ---------------------------------------------------------------------------

    def set_citations(
        self,
        owner_col: str,
        owner_id: UUID,
        document_id: UUID,
        source_text: str,
        quotes: Sequence[str],
    ) -> list[UUID]:
        """One citation per quote, by ordinal. Offsets are NULL when the quote isn't found.
        Ordinals past the end of `quotes` are deleted, so a shorter list leaves nothing stale."""
        if owner_col not in CITATION_OWNERS:
            raise ValueError(f"unknown citation owner {owner_col!r}")
        ids = []
        for ordinal, quote in enumerate(quotes):
            span = locate_quote(source_text, quote)
            start, end = span if span else (None, None)
            values = {
                owner_col: owner_id,
                "ordinal": ordinal,
                "document_id": document_id,
                "quote": quote,
                "start_offset": start,
                "end_offset": end,
            }
            ids.append(
                self._put(
                    Citation,
                    (owner_col, "ordinal"),
                    values,
                    index_where=text(f"{owner_col} IS NOT NULL"),
                )
            )
        owner = Citation.__table__.c[owner_col]
        self.session.execute(
            delete(Citation).where(owner == owner_id, Citation.ordinal >= len(quotes))
        )
        return ids

    # --- Links -------------------------------------------------------------------------------

    def link_finding_term(self, finding_id: UUID, term_id: UUID, is_deadline_basis: bool) -> UUID:
        values = {
            "finding_id": finding_id,
            "term_id": term_id,
            "is_deadline_basis": is_deadline_basis,
        }
        return self._put(FindingTerm, ("tenant_id", "finding_id", "term_id"), values)

    def link_finding_transaction(self, finding_id: UUID, transaction_id: UUID) -> UUID:
        values = {"finding_id": finding_id, "transaction_id": transaction_id}
        return self._put(FindingTransaction, ("tenant_id", "finding_id", "transaction_id"), values)

    def link_finding_invoice_fact(
        self, finding_id: UUID, invoice_fact_id: UUID, is_deadline_basis: bool
    ) -> UUID:
        values = {
            "finding_id": finding_id,
            "invoice_fact_id": invoice_fact_id,
            "is_deadline_basis": is_deadline_basis,
        }
        return self._put(FindingInvoiceFact, ("tenant_id", "finding_id", "invoice_fact_id"), values)

    def set_finding_links(
        self,
        finding_id: UUID,
        terms: Mapping[UUID, bool],
        transactions: Iterable[UUID],
        invoice_facts: Mapping[UUID, bool],
    ) -> None:
        """Make the finding's links exactly these (item -> is_deadline_basis): upsert each,
        delete the rest. A recomputed finding can drop an item it cited before."""
        txns = list(transactions)
        for tid, basis in terms.items():
            self.link_finding_term(finding_id, tid, basis)
        for xid in txns:
            self.link_finding_transaction(finding_id, xid)
        for fid, basis in invoice_facts.items():
            self.link_finding_invoice_fact(finding_id, fid, basis)
        for model, col, keep in (
            (FindingTerm, FindingTerm.term_id, list(terms)),
            (FindingTransaction, FindingTransaction.transaction_id, txns),
            (FindingInvoiceFact, FindingInvoiceFact.invoice_fact_id, list(invoice_facts)),
        ):
            stale = [model.tenant_id == self.tenant_id, model.finding_id == finding_id]
            if keep:
                stale.append(col.not_in(keep))
            self.session.execute(delete(model).where(*stale))

    def link_documents(
        self,
        document_id: UUID,
        linked_document_id: UUID,
        link_type: str,
        context_fingerprint: str | None = None,
    ) -> UUID:
        values = {
            "document_id": document_id,
            "linked_document_id": linked_document_id,
            "link_type": link_type,
            "context_fingerprint": context_fingerprint,
        }
        key = ("tenant_id", "document_id", "linked_document_id", "link_type")
        return self._put(DocumentLink, key, values)

    def prune_links(self, link_type: str, keep: set[tuple[UUID, UUID]]) -> None:
        """Delete this tenant's `link_type` links other than the (document, linked document)
        pairs in `keep`. For links that are recomputed whole (A6's duplicates)."""
        rows = self.session.execute(
            select(
                DocumentLink.id, DocumentLink.document_id, DocumentLink.linked_document_id
            ).where(DocumentLink.tenant_id == self.tenant_id, DocumentLink.link_type == link_type)
        ).all()
        stale = [r.id for r in rows if (r.document_id, r.linked_document_id) not in keep]
        if stale:
            self.session.execute(delete(DocumentLink).where(DocumentLink.id.in_(stale)))

    # --- Extractor scripts (Phase 4) ---------------------------------------------------------

    def add_extractor_script(
        self,
        name: str,
        code: str,
        matcher: str | None,
        accuracy: float,
        promoted_at: Any,
    ) -> ExtractorScript:
        """A new version of `name` (1, 2, ...), promoted or not: rejected candidates stay as
        the test record."""
        versions = self.session.scalars(
            select(ExtractorScript.version).where(
                ExtractorScript.tenant_id == self.tenant_id, ExtractorScript.name == name
            )
        ).all()
        version = str(max((int(v) for v in versions if v.isdigit()), default=0) + 1)
        row = ExtractorScript(
            tenant_id=self.tenant_id,
            name=name,
            version=version,
            matcher=matcher,
            code=code,
            accuracy=accuracy,
            promoted_at=promoted_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def promoted_scripts(self) -> list[ExtractorScript]:
        """The latest promoted version per (name, matcher), by name then matcher. The matcher
        is the sender, so two senders of one vendor (one name) each keep their script."""
        rows = self.session.scalars(
            select(ExtractorScript).where(
                ExtractorScript.tenant_id == self.tenant_id,
                ExtractorScript.promoted_at.is_not(None),
            )
        ).all()

        def version(r: ExtractorScript) -> int:
            return int(r.version) if r.version.isdigit() else 0

        latest: dict[tuple[str, str], ExtractorScript] = {}
        for r in rows:
            key = (r.name, r.matcher or "")
            if key not in latest or version(r) > version(latest[key]):
                latest[key] = r
        return [latest[k] for k in sorted(latest)]
