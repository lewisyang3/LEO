"""Response models for the API (ROADMAP C2, C3). API-only: the shared types stay in `leo.types`."""

import datetime
from typing import Annotated, Literal

from pydantic import StringConstraints

from leo.types import (
    ActionState,
    ActionType,
    DocumentClass,
    ExtractedTerm,
    Finding,
    FindingKind,
    InvoiceFact,
    LeoModel,
    Money,
    SourceDocument,
    Transaction,
    Vendor,
)


class QuoteRef(LeoModel):
    """A quote and its span in the raw source text, end exclusive. None when not found."""

    document: str
    quote: str
    start: int | None
    end: int | None


class Number(LeoModel):
    """A number shown on a finding, with the term and invoice-fact ids it comes from."""

    key: str  # "impact.annual", "computation.cap_pct", "deadline"
    label: str
    value: Money | int | datetime.date
    unit: Literal["usd", "pct", "count", "date"]
    sources: list[str]
    headline: bool = False  # the impact field that is the finding's dollars at stake


class TermOut(LeoModel):
    term: ExtractedTerm
    citations: list[QuoteRef]


class InvoiceFactOut(LeoModel):
    fact: InvoiceFact
    citations: list[QuoteRef]


class ActionOut(LeoModel):
    id: str
    finding: str
    finding_title: str
    vendor: str  # canonical name
    type: ActionType
    to: str
    subject: str
    body: str
    pre_step: str | None
    alternative: ActionType | None
    state: ActionState
    engine: str  # "llm:<model>" or "template"
    edited: bool = False
    sent_at: datetime.datetime | None = None
    deadline: datetime.date | None
    recommended_send_by: datetime.date | None


NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ActionEdit(LeoModel):
    to: NonBlank
    subject: NonBlank
    body: NonBlank


class FindingSummary(LeoModel):
    id: str
    kind: FindingKind
    vendor: str  # vendor id
    vendor_name: str
    title: str
    plan_label: str | None
    deadline: datetime.date | None
    recommended_send_by: datetime.date | None
    days_left: int | None  # to the send-by date (else the deadline); None without a deadline
    at_stake: Money
    action_id: str
    action_state: ActionState


class FindingDetail(LeoModel):
    summary: FindingSummary
    finding: Finding
    vendor: Vendor
    numbers: list[Number]
    terms: list[TermOut]
    invoice_facts: list[InvoiceFactOut]
    transactions: list[Transaction]
    documents: list[SourceDocument]
    linked_duplicates: list[str]
    action: ActionOut


class DocumentOut(LeoModel):
    path: str
    doc_class: DocumentClass | None
    vendor: str | None
    text: str


class Dashboard(LeoModel):
    today: datetime.date
    open_findings: int
    at_stake: Money
    pending_actions: int
