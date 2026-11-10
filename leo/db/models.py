"""Track A1 SQLAlchemy 2 mappings for Leo (PostgreSQL 16).

Fields documented in DATA_MODEL.md are implemented directly. Fields only
specified by table name in the roadmap are a *provisional minimal schema*;
compare those with your full design doc before generating the first migration.

Application-generated UUIDv7 IDs: PostgreSQL 16 has no built-in uuidv7().
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import secrets
import time
from uuid import UUID

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_uuid7() -> UUID:
    """Make an RFC 9562 UUIDv7 (48-bit Unix millisecond timestamp)."""
    ms = int(time.time() * 1000)
    if not 0 <= ms < (1 << 48):
        raise ValueError("Timestamp outside UUIDv7 range")
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    bits = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return UUID(int=bits)


class Base(DeclarativeBase):
    pass


class Row(Base):
    """Mixin for every row. Requires each concrete class to set __tablename__."""
    __abstract__ = True
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=new_uuid7)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)


class NaturalKeyRow(Row):
    __abstract__ = True
    natural_key: Mapped[str] = mapped_column(Text, nullable=False)
    # A uniqueness constraint is declared on each concrete table below.


class Tenant(Row):
    __tablename__ = "tenant"
    name: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (CheckConstraint("tenant_id = id", name="ck_tenant_self_id"),)
    # For tenant rows, tenant_id is the tenant's own id.


class Vendor(Row):
    __tablename__ = "vendor"
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    normalized_name: Mapped[str | None] = mapped_column(Text)
    # Per DATA_MODEL.md vendors have no content-hash natural key.


class VendorAlias(Row):
    __tablename__ = "vendor_alias"
    vendor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("vendor.id"), nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "normalized_alias", name="uq_vendor_alias_normalized"),)


class SourceDocument(NaturalKeyRow):
    __tablename__ = "source_document"
    vendor_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("vendor.id"))
    parent_document_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("source_document.id"))
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_class: Mapped[str | None] = mapped_column(String(64))
    sender: Mapped[str | None] = mapped_column(Text)
    recipient: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)  # metadata, never identity
    thread_id: Mapped[str | None] = mapped_column(Text)   # metadata, never identity
    filename: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)  # source document path in shared types
    notes: Mapped[str | None] = mapped_column(Text)
    exact_duplicate_of_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("source_document.id"))
    probable_duplicate_of_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("source_document.id"))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    classification_raw: Mapped[dict | None] = mapped_column(JSONB)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "natural_key", name="uq_source_document_nk"),)


class Transaction(NaturalKeyRow):
    __tablename__ = "transaction"
    vendor_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("vendor.id"))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    descriptor: Mapped[str] = mapped_column(Text, nullable=False)
    card_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    seats_billed: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    date: Mapped[date] = mapped_column(Date, nullable=False)
    plan_label: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("tenant_id", "natural_key", name="uq_transaction_nk"),)


class ContractTerm(NaturalKeyRow):
    __tablename__ = "contract_term"
    vendor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("vendor.id"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("source_document.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    plan_label: Mapped[str | None] = mapped_column(Text)
    agreement_ref: Mapped[str | None] = mapped_column(Text)
    effective_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="current")
    superseded_by: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("contract_term.id"))
    supersedes: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("contract_term.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    raw_model_output: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("tenant_id", "natural_key", name="uq_contract_term_nk"),
        CheckConstraint("type IN ('price','price_cap','escalator','renewal','notice_window','seat_count','dispute_window')", name="ck_contract_term_type"),
        CheckConstraint("status IN ('current','superseded','needs_review')", name="ck_contract_term_status"),
    )


class InvoiceFact(NaturalKeyRow):
    __tablename__ = "invoice_fact"
    vendor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("vendor.id"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("source_document.id"), nullable=False)
    billing_period: Mapped[str] = mapped_column(Text, nullable=False)  # YYYY-MM or YYYY-MM-DD/YYYY-MM-DD
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    invoice_number: Mapped[str | None] = mapped_column(Text)
    seats_billed: Mapped[int | None] = mapped_column(Integer)
    matches_transaction: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("transaction.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="current")
    raw_model_output: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("tenant_id", "natural_key", name="uq_invoice_fact_nk"),
        CheckConstraint("billing_period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$' OR billing_period ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]{4}-[0-9]{2}-[0-9]{2}$'", name="ck_invoice_period"),
        CheckConstraint("status IN ('current','needs_review')", name="ck_invoice_status"),
    )


class VendorContact(Row):
    __tablename__ = "vendor_contact"
    vendor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("vendor.id"), nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "vendor_id", "email", "purpose", name="uq_vendor_contact_identity"),)


class Citation(Row):
    __tablename__ = "citation"
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("source_document.id"), nullable=False)
    term_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("contract_term.id"))
    invoice_fact_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("invoice_fact.id"))
    vendor_contact_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("vendor_contact.id"))
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        CheckConstraint("num_nonnulls(term_id, invoice_fact_id, vendor_contact_id) = 1", name="ck_citation_one_owner"),
        CheckConstraint("(start_offset IS NULL AND end_offset IS NULL) OR (start_offset IS NOT NULL AND end_offset IS NOT NULL AND start_offset >= 0 AND end_offset - start_offset = char_length(quote))", name="ck_citation_span"),
        CheckConstraint("ordinal >= 0", name="ck_citation_ordinal"),
        Index("uq_citation_term_ordinal", "term_id", "ordinal", unique=True, postgresql_where=text("term_id IS NOT NULL")),
        Index("uq_citation_invoice_ordinal", "invoice_fact_id", "ordinal", unique=True, postgresql_where=text("invoice_fact_id IS NOT NULL")),
        Index("uq_citation_contact_ordinal", "vendor_contact_id", "ordinal", unique=True, postgresql_where=text("vendor_contact_id IS NOT NULL")),
        Index("uq_citation_one_per_contact", "vendor_contact_id", unique=True, postgresql_where=text("vendor_contact_id IS NOT NULL")),
    )
    # A database CHECK cannot enforce 'at least one citation per contact'; repository writes must do so.


class Finding(NaturalKeyRow):
    __tablename__ = "finding"
    vendor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("vendor.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    plan_label: Mapped[str | None] = mapped_column(Text)
    computation: Mapped[dict | None] = mapped_column(JSONB)
    anchor_term_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("contract_term.id"), nullable=False)
    anchor_transaction_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("transaction.id"))
    deadline: Mapped[date | None] = mapped_column(Date)
    recommended_send_by: Mapped[date | None] = mapped_column(Date)
    deadline_notes: Mapped[str | None] = mapped_column(Text)
    impact: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="current")
    __table_args__ = (
        UniqueConstraint("tenant_id", "natural_key", name="uq_finding_nk"),
        CheckConstraint("kind IN ('auto_renewal_deadline','price_increase_notice','price_cap_exceeded','seats_over_commitment','escalator_applied_early')", name="ck_finding_kind"),
    )


class FindingTerm(Row):
    __tablename__ = "finding_term"
    finding_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("finding.id"), nullable=False)
    term_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("contract_term.id"), nullable=False)
    is_deadline_basis: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (UniqueConstraint("tenant_id", "finding_id", "term_id", name="uq_finding_term"),)


class FindingTransaction(Row):
    __tablename__ = "finding_transaction"
    finding_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("finding.id"), nullable=False)
    transaction_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("transaction.id"), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "finding_id", "transaction_id", name="uq_finding_transaction"),)


class FindingInvoiceFact(Row):
    __tablename__ = "finding_invoice_fact"
    finding_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("finding.id"), nullable=False)
    invoice_fact_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("invoice_fact.id"), nullable=False)
    is_deadline_basis: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (UniqueConstraint("tenant_id", "finding_id", "invoice_fact_id", name="uq_finding_invoice_fact"),)


class DocumentLink(Row):
    __tablename__ = "document_link"
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("source_document.id"), nullable=False)
    linked_document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("source_document.id"), nullable=False)
    link_type: Mapped[str] = mapped_column(String(32), nullable=False, default="duplicate")
    context_fingerprint: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", "linked_document_id", "link_type", name="uq_document_link"),
        CheckConstraint("document_id <> linked_document_id", name="ck_document_link_distinct"),
    )


class Action(NaturalKeyRow):
    __tablename__ = "action"
    finding_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("finding.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    pre_step: Mapped[str | None] = mapped_column(Text)
    alternative: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    raw_model_output: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("tenant_id", "natural_key", name="uq_action_nk"),)


class ExtractorScript(Row):
    __tablename__ = "extractor_script"
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    matcher: Mapped[str | None] = mapped_column(Text)
    parser: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("tenant_id", "name", "version", name="uq_extractor_script_version"),)


class FeedbackEvent(Row):
    __tablename__ = "feedback_event"
    finding_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("finding.id"))
    action_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("action.id"))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Job(NaturalKeyRow):
    __tablename__ = "job"
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("source_document.id"), nullable=False)
    stage: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    processed_by: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    raw_model_output: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (UniqueConstraint("tenant_id", "natural_key", name="uq_job_nk"),)
