"""The repository: every write is an upsert (ROADMAP A2).

Each write is `INSERT ... ON CONFLICT DO UPDATE ... RETURNING id` on the row's natural key or
unique constraint, so a rerun returns the existing id and never creates a duplicate. Ids are
never updated. On conflict, each table's columns fall in three groups:

- identity and provenance (`insert_only`): the first writer wins. D1's re-synced copy keeps
  the original's path.
- derived values (everything else passed in): overwritten from the new write.
- user-owned (`action.state`, `approver`, `decided_at`): never written by an upsert. An
  action's draft content only updates while it is still a draft, so approvals survive reruns.

The caller owns the transaction; `Repo` never commits.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leo.db import keys
from leo.db.models import (
    Action,
    Base,
    Citation,
    ContractTerm,
    DocumentLink,
    Finding,
    FindingInvoiceFact,
    FindingTerm,
    FindingTransaction,
    InvoiceFact,
    Job,
    SourceDocument,
    Tenant,
    Transaction,
    Vendor,
    VendorAlias,
    VendorContact,
    new_uuid7,
)
from leo.rules import locate_quote
from leo.types import ActionState

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
)
ACTION_USER_OWNED = ("state", "approver", "decided_at")
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

    def upsert_transaction(self, natural_key: str, **cols: Any) -> UUID:
        return self._put(Transaction, NK, {"natural_key": natural_key, **cols})

    def upsert_term(self, natural_key: str, **cols: Any) -> UUID:
        return self._put(ContractTerm, NK, {"natural_key": natural_key, **cols})

    def upsert_invoice_fact(self, natural_key: str, **cols: Any) -> UUID:
        return self._put(InvoiceFact, NK, {"natural_key": natural_key, **cols})

    def upsert_finding(self, natural_key: str, **cols: Any) -> UUID:
        return self._put(Finding, NK, {"natural_key": natural_key, **cols})

    def upsert_action(self, natural_key: str, **cols: Any) -> UUID:
        """Draft content updates only while the action is a draft; state is the user's."""
        if owned := set(cols) & set(ACTION_USER_OWNED):
            raise ValueError(f"upsert_action never writes user-owned columns: {sorted(owned)}")
        values = {"natural_key": natural_key, **cols}
        return self._put(Action, NK, values, where=Action.state == ActionState.draft)

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
