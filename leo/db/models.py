"""SQLAlchemy 2 mappings for LEO (PostgreSQL 16), roadmap A1.

Columns follow docs/DATA_MODEL.md and the design doc's entity table. CHECK value lists come
from the enums in leo.types, so the schema and the shared types can't drift.

Tenant isolation is enforced by the database: every table has `tenant_id` (FK to `tenant`)
and a `UNIQUE (tenant_id, id)`, and every cross-table reference is a composite foreign key
on `(tenant_id, <col>)`, so a row can only point at rows of its own tenant.

IDs are application-generated UUIDv7s: PostgreSQL 16 has no built-in uuidv7().
"""

from __future__ import annotations

import datetime as dt
import secrets
import time
from collections.abc import Iterable
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from leo.types import ActionState, ActionType, FindingKind, TermStatus, TermType

# Finding statuses have no shared enum yet. C1 recomputes findings; the user's decision
# lives on action.state.
FINDING_STATUSES = ("current", "superseded")
INVOICE_STATUSES = (TermStatus.current, TermStatus.needs_review)
JOB_STATUSES = ("pending", "running", "done", "failed", "parked")


def new_uuid7() -> UUID:
    """Make an RFC 9562 UUIDv7 (48-bit Unix millisecond timestamp)."""
    ms = int(time.time() * 1000)
    if not 0 <= ms < (1 << 48):
        raise ValueError("Timestamp outside UUIDv7 range")
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    bits = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return UUID(int=bits)


def _in(col: str, values: Iterable[str]) -> str:
    """SQL `col IN ('a', 'b', ...)` for a CHECK constraint."""
    return f"{col} IN ({', '.join(f"'{v}'" for v in map(str, values))})"


def _check_in(table: str, col: str, values: Iterable[str]) -> CheckConstraint:
    return CheckConstraint(_in(col, values), name=f"ck_{table}_{col}")


def _fk(table: str, col: str, target: str, *, deferred: bool = False) -> ForeignKeyConstraint:
    """Composite FK `(tenant_id, col) -> target(tenant_id, id)`, so references stay in-tenant.

    `deferred` checks at commit, for self-references that may be written in either order
    (a term and the term it supersedes point at each other).
    """
    extra: dict[str, Any] = {"deferrable": True, "initially": "DEFERRED"} if deferred else {}
    return ForeignKeyConstraint(
        ["tenant_id", col],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_{table}_{col}",
        **extra,
    )


def _tenant_id_key(table: str) -> UniqueConstraint:
    """`UNIQUE (tenant_id, id)`: the target of every composite FK into this table."""
    return UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id")


def _nk(table: str) -> UniqueConstraint:
    return UniqueConstraint("tenant_id", "natural_key", name=f"uq_{table}_nk")


class Base(DeclarativeBase):
    pass


class Row(Base):
    """Columns every table has. Each concrete class sets __tablename__ and __table_args__."""

    __abstract__ = True
    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenant.id"), nullable=False)


class NaturalKeyRow(Row):
    __abstract__ = True
    natural_key: Mapped[str] = mapped_column(Text, nullable=False)


class Tenant(Row):
    """A tenant row's tenant_id is its own id."""

    __tablename__ = "tenant"
    slug: Mapped[str] = mapped_column(Text, nullable=False)  # natural key, e.g. the dataset
    name: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        _tenant_id_key("tenant"),
        UniqueConstraint("slug", name="uq_tenant_slug"),
        CheckConstraint("tenant_id = id", name="ck_tenant_self_id"),
    )

    def __init__(self, **kwargs: Any) -> None:
        if kwargs.get("id") is None:
            kwargs["id"] = new_uuid7()
        kwargs.setdefault("tenant_id", kwargs["id"])
        super().__init__(**kwargs)


class Vendor(Row):
    """Vendors have no hash natural key (DATA_MODEL.md); they upsert on normalized_name."""

    __tablename__ = "vendor"
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        _tenant_id_key("vendor"),
        UniqueConstraint("tenant_id", "normalized_name", name="uq_vendor_normalized_name"),
    )


class VendorAlias(Row):
    __tablename__ = "vendor_alias"
    vendor_id: Mapped[UUID] = mapped_column(nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        _tenant_id_key("vendor_alias"),
        _fk("vendor_alias", "vendor_id", "vendor"),
        UniqueConstraint("tenant_id", "normalized_alias", name="uq_vendor_alias_normalized"),
    )


class SourceDocument(NaturalKeyRow):
    __tablename__ = "source_document"
    vendor_id: Mapped[UUID | None]
    parent_document_id: Mapped[UUID | None]
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_class: Mapped[str | None] = mapped_column(String(64))
    sender: Mapped[str | None] = mapped_column(Text)
    recipient: Mapped[str | None] = mapped_column(Text)  # null for contract files
    subject: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)  # metadata, never identity
    thread_id: Mapped[str | None] = mapped_column(Text)  # metadata, never identity
    filename: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)  # source document path in shared types
    notes: Mapped[str | None] = mapped_column(Text)
    exact_duplicate_of_id: Mapped[UUID | None]
    probable_duplicate_of_id: Mapped[UUID | None]
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    classification_raw: Mapped[dict | None] = mapped_column(JSONB)
    received_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        _tenant_id_key("source_document"),
        _nk("source_document"),
        _fk("source_document", "vendor_id", "vendor"),
        _fk("source_document", "parent_document_id", "source_document", deferred=True),
        _fk("source_document", "exact_duplicate_of_id", "source_document", deferred=True),
        _fk("source_document", "probable_duplicate_of_id", "source_document", deferred=True),
    )


class Transaction(NaturalKeyRow):
    __tablename__ = "transaction"
    vendor_id: Mapped[UUID | None]
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    descriptor: Mapped[str] = mapped_column(Text, nullable=False)
    card_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    seats_billed: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    plan_label: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        _tenant_id_key("transaction"),
        _nk("transaction"),
        _fk("transaction", "vendor_id", "vendor"),
    )


class ContractTerm(NaturalKeyRow):
    __tablename__ = "contract_term"
    vendor_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    plan_label: Mapped[str | None] = mapped_column(Text)
    agreement_ref: Mapped[str | None] = mapped_column(Text)
    effective_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=TermStatus.current)
    superseded_by: Mapped[UUID | None]
    supersedes: Mapped[UUID | None]
    notes: Mapped[str | None] = mapped_column(Text)
    raw_model_output: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        _tenant_id_key("contract_term"),
        _nk("contract_term"),
        _fk("contract_term", "vendor_id", "vendor"),
        _fk("contract_term", "document_id", "source_document"),
        _fk("contract_term", "superseded_by", "contract_term", deferred=True),
        _fk("contract_term", "supersedes", "contract_term", deferred=True),
        _check_in("contract_term", "type", TermType),
        _check_in("contract_term", "status", TermStatus),
    )


class InvoiceFact(NaturalKeyRow):
    __tablename__ = "invoice_fact"
    vendor_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    # YYYY-MM or YYYY-MM-DD/YYYY-MM-DD
    billing_period: Mapped[str] = mapped_column(Text, nullable=False)
    invoice_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    invoice_number: Mapped[str | None] = mapped_column(Text)
    seats_billed: Mapped[int | None] = mapped_column(Integer)
    matches_transaction: Mapped[UUID | None]
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=TermStatus.current)
    raw_model_output: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        _tenant_id_key("invoice_fact"),
        _nk("invoice_fact"),
        _fk("invoice_fact", "vendor_id", "vendor"),
        _fk("invoice_fact", "document_id", "source_document"),
        _fk("invoice_fact", "matches_transaction", "transaction"),
        CheckConstraint(
            "billing_period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'"
            " OR billing_period ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]{4}-[0-9]{2}-[0-9]{2}$'",
            name="ck_invoice_period",
        ),
        _check_in("invoice_fact", "status", INVOICE_STATUSES),
    )


class VendorContact(Row):
    __tablename__ = "vendor_contact"
    vendor_id: Mapped[UUID] = mapped_column(nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        _tenant_id_key("vendor_contact"),
        _fk("vendor_contact", "vendor_id", "vendor"),
        UniqueConstraint(
            "tenant_id", "vendor_id", "email", "purpose", name="uq_vendor_contact_identity"
        ),
    )


class Citation(Row):
    """Its owner is exactly one of term, invoice fact or vendor contact.

    A CHECK can't enforce "at least one citation per contact"; repository writes must.
    """

    __tablename__ = "citation"
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    term_id: Mapped[UUID | None]
    invoice_fact_id: Mapped[UUID | None]
    vendor_contact_id: Mapped[UUID | None]
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        _tenant_id_key("citation"),
        _fk("citation", "document_id", "source_document"),
        _fk("citation", "term_id", "contract_term"),
        _fk("citation", "invoice_fact_id", "invoice_fact"),
        _fk("citation", "vendor_contact_id", "vendor_contact"),
        CheckConstraint(
            "num_nonnulls(term_id, invoice_fact_id, vendor_contact_id) = 1",
            name="ck_citation_one_owner",
        ),
        CheckConstraint(
            "(start_offset IS NULL AND end_offset IS NULL)"
            " OR (start_offset IS NOT NULL AND end_offset IS NOT NULL AND start_offset >= 0"
            " AND end_offset - start_offset = char_length(quote))",
            name="ck_citation_span",
        ),
        CheckConstraint("ordinal >= 0", name="ck_citation_ordinal"),
        Index(
            "uq_citation_term_ordinal",
            "term_id",
            "ordinal",
            unique=True,
            postgresql_where=text("term_id IS NOT NULL"),
        ),
        Index(
            "uq_citation_invoice_ordinal",
            "invoice_fact_id",
            "ordinal",
            unique=True,
            postgresql_where=text("invoice_fact_id IS NOT NULL"),
        ),
        Index(
            "uq_citation_contact_ordinal",
            "vendor_contact_id",
            "ordinal",
            unique=True,
            postgresql_where=text("vendor_contact_id IS NOT NULL"),
        ),
        Index(
            "uq_citation_one_per_contact",
            "vendor_contact_id",
            unique=True,
            postgresql_where=text("vendor_contact_id IS NOT NULL"),
        ),
    )


class Finding(NaturalKeyRow):
    __tablename__ = "finding"
    vendor_id: Mapped[UUID] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    plan_label: Mapped[str | None] = mapped_column(Text)
    computation: Mapped[dict | None] = mapped_column(JSONB)
    anchor_term_id: Mapped[UUID] = mapped_column(nullable=False)
    anchor_transaction_id: Mapped[UUID | None]
    deadline: Mapped[dt.date | None] = mapped_column(Date)
    recommended_send_by: Mapped[dt.date | None] = mapped_column(Date)
    deadline_notes: Mapped[str | None] = mapped_column(Text)
    impact: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="current")
    __table_args__ = (
        _tenant_id_key("finding"),
        _nk("finding"),
        _fk("finding", "vendor_id", "vendor"),
        _fk("finding", "anchor_term_id", "contract_term"),
        _fk("finding", "anchor_transaction_id", "transaction"),
        _check_in("finding", "kind", FindingKind),
        _check_in("finding", "status", FINDING_STATUSES),
    )


class FindingTerm(Row):
    __tablename__ = "finding_term"
    finding_id: Mapped[UUID] = mapped_column(nullable=False)
    term_id: Mapped[UUID] = mapped_column(nullable=False)
    is_deadline_basis: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        _tenant_id_key("finding_term"),
        _fk("finding_term", "finding_id", "finding"),
        _fk("finding_term", "term_id", "contract_term"),
        UniqueConstraint("tenant_id", "finding_id", "term_id", name="uq_finding_term"),
    )


class FindingTransaction(Row):
    __tablename__ = "finding_transaction"
    finding_id: Mapped[UUID] = mapped_column(nullable=False)
    transaction_id: Mapped[UUID] = mapped_column(nullable=False)
    __table_args__ = (
        _tenant_id_key("finding_transaction"),
        _fk("finding_transaction", "finding_id", "finding"),
        _fk("finding_transaction", "transaction_id", "transaction"),
        UniqueConstraint(
            "tenant_id", "finding_id", "transaction_id", name="uq_finding_transaction"
        ),
    )


class FindingInvoiceFact(Row):
    __tablename__ = "finding_invoice_fact"
    finding_id: Mapped[UUID] = mapped_column(nullable=False)
    invoice_fact_id: Mapped[UUID] = mapped_column(nullable=False)
    is_deadline_basis: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        _tenant_id_key("finding_invoice_fact"),
        _fk("finding_invoice_fact", "finding_id", "finding"),
        _fk("finding_invoice_fact", "invoice_fact_id", "invoice_fact"),
        UniqueConstraint(
            "tenant_id", "finding_id", "invoice_fact_id", name="uq_finding_invoice_fact"
        ),
    )


class DocumentLink(Row):
    __tablename__ = "document_link"
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    linked_document_id: Mapped[UUID] = mapped_column(nullable=False)
    link_type: Mapped[str] = mapped_column(String(32), nullable=False, default="duplicate")
    context_fingerprint: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        _tenant_id_key("document_link"),
        _fk("document_link", "document_id", "source_document"),
        _fk("document_link", "linked_document_id", "source_document"),
        UniqueConstraint(
            "tenant_id", "document_id", "linked_document_id", "link_type", name="uq_document_link"
        ),
        CheckConstraint("document_id <> linked_document_id", name="ck_document_link_distinct"),
    )


class Action(NaturalKeyRow):
    """A drafted notice. Approved and sent actions must record who approved them."""

    __tablename__ = "action"
    finding_id: Mapped[UUID] = mapped_column(nullable=False)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    pre_step: Mapped[str | None] = mapped_column(Text)
    alternative: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default=ActionState.draft)
    approver: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    edited_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))  # C3
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))  # C3
    raw_model_output: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        _tenant_id_key("action"),
        _nk("action"),
        _fk("action", "finding_id", "finding"),
        _check_in("action", "action_type", ActionType),
        _check_in("action", "state", ActionState),
        CheckConstraint(
            f"NOT ({_in('state', (ActionState.approved, ActionState.sent))})"
            " OR approver IS NOT NULL",
            name="ck_action_approver",
        ),
    )


class ExtractorScript(Row):
    """A parser for one known format. Promoted only when it matches past verified results."""

    __tablename__ = "extractor_script"
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    matcher: Mapped[str | None] = mapped_column(Text)
    parser: Mapped[str | None] = mapped_column(Text)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    accuracy: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    promoted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        _tenant_id_key("extractor_script"),
        UniqueConstraint("tenant_id", "name", "version", name="uq_extractor_script_version"),
        CheckConstraint("accuracy >= 0 AND accuracy <= 1", name="ck_extractor_script_accuracy"),
    )


class FeedbackEvent(Row):
    """A user correction or rejection: the training signal for prompts and scripts."""

    __tablename__ = "feedback_event"
    term_id: Mapped[UUID | None]
    finding_id: Mapped[UUID | None]
    action_id: Mapped[UUID | None]
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    field: Mapped[str | None] = mapped_column(Text)
    old_value: Mapped[Any | None] = mapped_column(JSONB)
    new_value: Mapped[Any | None] = mapped_column(JSONB)
    reviewer: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        _tenant_id_key("feedback_event"),
        _fk("feedback_event", "term_id", "contract_term"),
        _fk("feedback_event", "finding_id", "finding"),
        _fk("feedback_event", "action_id", "action"),
    )


class Job(NaturalKeyRow):
    """A queue row, claimed with FOR UPDATE SKIP LOCKED (A4)."""

    __tablename__ = "job"
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    stage: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    processed_by: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    raw_model_output: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # A retried job waits until then; NULL means it can run now (A4).
    run_after: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        _tenant_id_key("job"),
        _nk("job"),
        _fk("job", "document_id", "source_document"),
        _check_in("job", "status", JOB_STATUSES),
        Index("ix_job_claim", "status", "created_at"),
    )
