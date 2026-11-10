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
    action_id: str | None  # None until the pipeline drafts one (2.1)
    action_state: ActionState | None


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
    action: ActionOut | None  # the finding's primary (oldest) action; None until drafted


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


class UploadOut(LeoModel):
    """One uploaded file. `path` is the stored row's path: bytes already ingested keep theirs."""

    document_id: str
    path: str
    created: bool  # False when these bytes were already ingested
    attachments: list[str]


class JobCount(LeoModel):
    stage: str
    processed_by: str | None
    status: str
    count: int


class Stats(LeoModel):
    """Counts from the database (ROADMAP C3). Cost numbers come with B6."""

    documents: int
    documents_by_class: dict[str, int]  # "unclassified" when not yet classified
    terms_by_status: dict[str, int]
    invoice_facts_by_status: dict[str, int]
    findings: int
    actions_by_state: dict[str, int]
    jobs: list[JobCount]


class VendorSummary(LeoModel):
    """One row of the vendor list (ROADMAP C5). Counts leave out linked duplicates' terms."""

    id: str
    canonical_name: str
    aliases: list[str]
    terms: int
    transactions: int
    open_findings: int
    at_stake: Money


class PricePoint(LeoModel):
    term: TermOut
    in_effect: bool  # effective on or before today (an undated price counts as in effect)
    change: Money | None  # amount minus the previous point's; None first or when units differ


class PriceSeries(LeoModel):
    """A price history: one agreement and plan, by effective date."""

    agreement_ref: str | None
    plan_label: str | None
    points: list[PricePoint]


class MatchedTransaction(LeoModel):
    transaction: Transaction
    plan_label: str | None
    invoice_fact: InvoiceFact | None  # the invoice that matches this charge
    findings: list[str]  # ids of current findings that cite it


class VendorDetail(LeoModel):
    vendor: Vendor
    findings: list[FindingSummary]  # dashboard order
    price_history: list[PriceSeries]
    terms: list[TermOut]  # every status; linked duplicates' terms left out
    transactions: list[MatchedTransaction]  # by date
    linked_duplicates: list[str]  # paths of this vendor's documents counted under another
