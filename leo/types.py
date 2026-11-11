"""Shared types every stage codes against.

The shapes are copied from the answer key, `data/synthetic/expected.json` (schema v2). They
are documented in `data/synthetic/README.md` and enforced by `scripts/validate_answer_key.py`.
The fixture loader reads the key into these models with no field mapping, so field names
match the key exactly.

Conventions:
- Cross-references are string ids as in the key: vendor id, document path, term id, transaction
  id, invoice-fact id.
- Money and percentages are `Decimal` and serialize to JSON numbers.
- A term value has exactly the keys for its type. Every key is required, and it is `None` when
  the document doesn't say.
"""

import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    field_validator,
)

Money = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]
Pct = Money


class LeoModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Enums ---------------------------------------------------------------------------------


class TermType(StrEnum):
    price = "price"
    price_cap = "price_cap"
    escalator = "escalator"
    renewal = "renewal"
    notice_window = "notice_window"
    seat_count = "seat_count"
    dispute_window = "dispute_window"


class PricePer(StrEnum):
    year = "year"
    month = "month"
    seat_month = "seat_month"


class RelativeTo(StrEnum):
    term_end = "term_end"
    renewal_date = "renewal_date"
    price_change = "price_change"
    invoice_date = "invoice_date"


class AppliesTo(StrEnum):
    non_renewal = "non_renewal"
    vendor_price_change = "vendor_price_change"
    invoice = "invoice"
    renewal_term = "renewal_term"


class SeatUnit(StrEnum):
    users = "users"
    seats = "seats"
    employees = "employees"


class TermStatus(StrEnum):
    current = "current"
    superseded = "superseded"
    needs_review = "needs_review"
    rejected = "rejected"  # a reviewer said this isn't an item (feedback loop)


# Items the ledger's consumers (findings, plan matching, priors, price history) leave out:
# failed validation, or rejected by a reviewer.
EXCLUDED_STATUSES = frozenset({TermStatus.needs_review, TermStatus.rejected})


class DocumentClass(StrEnum):
    contract = "contract"
    order_form = "order_form"
    price_notice = "price_notice"
    invoice = "invoice"
    receipt = "receipt"
    noise = "noise"


class FindingKind(StrEnum):
    auto_renewal_deadline = "auto_renewal_deadline"
    price_increase_notice = "price_increase_notice"
    price_cap_exceeded = "price_cap_exceeded"
    seats_over_commitment = "seats_over_commitment"
    escalator_applied_early = "escalator_applied_early"


class ActionType(StrEnum):
    cancellation_notice = "cancellation_notice"
    renegotiation_request = "renegotiation_request"
    negotiation_email = "negotiation_email"
    billing_dispute = "billing_dispute"


class ActionState(StrEnum):
    draft = "draft"
    approved = "approved"
    rejected = "rejected"
    sent = "sent"


# --- Term values (one per type; no defaults, so every key must be present) ------------------


class PriceValue(LeoModel):
    amount: Money | None
    currency: str | None
    per: PricePer | None


class PriceCapValue(LeoModel):
    max_increase_pct: Pct | None
    applies_to: AppliesTo | None
    baseline: str | None


class EscalatorValue(LeoModel):
    pct: Pct | None
    schedule: str | None
    first_application: datetime.date | None


class RenewalValue(LeoModel):
    auto_renew: bool | None
    initial_term_end: datetime.date | None
    current_term_end: datetime.date | None
    renewal_length_months: int | None
    renewal_pricing: str | None
    early_termination_fee: str | None


class NoticeWindowValue(LeoModel):
    days: int | None
    relative_to: RelativeTo | None
    applies_to: AppliesTo | None
    deliver_to: str | None
    next_deadline: datetime.date | None


class SeatCountValue(LeoModel):
    included: int | None
    unit: SeatUnit | None
    overage_per_unit_month: Money | None
    increase_requires: str | None


class DisputeWindowValue(LeoModel):
    days: int | None
    relative_to: RelativeTo | None
    applies_to: AppliesTo | None
    deliver_to: str | None


# --- Terms -----------------------------------------------------------------------------------


class _TermBase(LeoModel):
    id: str
    vendor: str
    document: str
    quotes: list[str] = Field(min_length=1)
    plan_label: str | None = None
    agreement_ref: str | None = None
    effective_date: datetime.date | None = None
    status: TermStatus = TermStatus.current
    superseded_by: str | None = None
    supersedes: str | None = None
    old_value: PriceValue | None = None
    notes: str | None = None


class PriceTerm(_TermBase):
    type: Literal[TermType.price]
    value: PriceValue


class PriceCapTerm(_TermBase):
    type: Literal[TermType.price_cap]
    value: PriceCapValue


class EscalatorTerm(_TermBase):
    type: Literal[TermType.escalator]
    value: EscalatorValue


class RenewalTerm(_TermBase):
    type: Literal[TermType.renewal]
    value: RenewalValue


class NoticeWindowTerm(_TermBase):
    type: Literal[TermType.notice_window]
    value: NoticeWindowValue


class SeatCountTerm(_TermBase):
    type: Literal[TermType.seat_count]
    value: SeatCountValue


class DisputeWindowTerm(_TermBase):
    type: Literal[TermType.dispute_window]
    value: DisputeWindowValue


ExtractedTerm = Annotated[
    PriceTerm
    | PriceCapTerm
    | EscalatorTerm
    | RenewalTerm
    | NoticeWindowTerm
    | SeatCountTerm
    | DisputeWindowTerm,
    Field(discriminator="type"),
]
extracted_term_adapter: TypeAdapter[ExtractedTerm] = TypeAdapter(ExtractedTerm)


# --- Records ---------------------------------------------------------------------------------


class Citation(LeoModel):
    """A quote located in its source. Offsets index the raw stored file text."""

    document: str
    quote: str
    start: int
    end: int


class SourceDocument(LeoModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str
    doc_class: DocumentClass | None = Field(alias="class")  # None: not yet classified
    vendor: str | None
    sha256: str | None = None
    exact_duplicate_of: str | None = None
    probable_duplicate_of: str | None = None
    notes: str | None = None


class InvoiceFact(LeoModel):
    """What a vendor billed, as opposed to what was agreed (a term)."""

    id: str
    vendor: str
    document: str
    billing_period: str  # "2026-09" or "2026-10-05/2026-11-04"
    invoice_date: datetime.date
    amount: Money
    quotes: list[str] = Field(min_length=1)
    invoice_number: str | None = None
    seats_billed: int | None = None
    matches_transaction: str | None = None
    status: TermStatus = TermStatus.current  # needs_review when validation fails
    notes: str | None = None


class Contact(LeoModel):
    email: str
    purpose: str
    document: str
    quote: str


class Vendor(LeoModel):
    id: str
    canonical_name: str
    aliases: list[str] = []
    contacts: list[Contact] = []
    notes: str | None = None


class Transaction(LeoModel):
    id: str
    date: datetime.date
    descriptor: str
    amount: Money
    currency: str
    card_last4: str
    seats_billed: int | None = None

    @field_validator("seats_billed", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        return None if v == "" else v


# --- Findings --------------------------------------------------------------------------------


class Anchor(LeoModel):
    """The term and transaction a finding's natural key is built from."""

    term: str
    transaction: str | None


class DeadlineBasis(LeoModel):
    """The terms and invoice fact a finding's deadline is computed from."""

    terms: list[str]
    invoice_fact: str | None


class AutoRenewalImpact(LeoModel):
    amount: Money
    basis: str

    @property
    def headline_amount(self) -> Decimal:
        return self.amount


class PriceIncreaseImpact(LeoModel):
    monthly: Money
    annual: Money
    pct: Pct

    @property
    def headline_amount(self) -> Decimal:
        return self.annual


class PriceCapImpact(LeoModel):
    overcharged_to_date: Money
    remaining_renewal_term: Money
    full_renewal_term: Money

    @property
    def headline_amount(self) -> Decimal:
        return self.full_renewal_term


class PriceCapComputation(LeoModel):
    prior_monthly: Money
    cap_pct: Pct
    max_allowed_monthly: Money
    charged_monthly: Money
    actual_increase_pct: Pct
    overcharge_monthly: Money


class SeatsImpact(LeoModel):
    overcharged_to_date: Money
    annualized: Money

    @property
    def headline_amount(self) -> Decimal:
        return self.annualized


class SeatsComputation(LeoModel):
    contracted_seats: int
    billed_seats: int
    extra_seats: int
    price_per_seat_month: Money
    overcharge_monthly: Money


class EscalatorImpact(LeoModel):
    overcharged_to_date: Money
    if_uncorrected_until_anniversary: Money

    @property
    def headline_amount(self) -> Decimal:
        return self.if_uncorrected_until_anniversary


class EscalatorComputation(LeoModel):
    contract_monthly: Money
    charged_monthly: Money
    escalator_pct: Pct
    correct_first_application: datetime.date
    applied_from: datetime.date
    overcharge_monthly: Money


class _FindingBase(LeoModel):
    id: str
    vendor: str
    title: str
    terms: list[str]
    transactions: list[str]
    documents: list[str]
    invoice_facts: list[str] = []
    linked_duplicates: list[str] = []
    plan_label: str | None = None
    anchor: Anchor
    deadline: datetime.date | None
    recommended_send_by: datetime.date | None
    deadline_basis: DeadlineBasis | None
    deadline_notes: str | None


class AutoRenewalFinding(_FindingBase):
    kind: Literal[FindingKind.auto_renewal_deadline]
    impact: AutoRenewalImpact


class PriceIncreaseFinding(_FindingBase):
    kind: Literal[FindingKind.price_increase_notice]
    impact: PriceIncreaseImpact


class PriceCapExceededFinding(_FindingBase):
    kind: Literal[FindingKind.price_cap_exceeded]
    impact: PriceCapImpact
    computation: PriceCapComputation


class SeatsOverCommitmentFinding(_FindingBase):
    kind: Literal[FindingKind.seats_over_commitment]
    impact: SeatsImpact
    computation: SeatsComputation


class EscalatorAppliedEarlyFinding(_FindingBase):
    kind: Literal[FindingKind.escalator_applied_early]
    impact: EscalatorImpact
    computation: EscalatorComputation


Finding = Annotated[
    AutoRenewalFinding
    | PriceIncreaseFinding
    | PriceCapExceededFinding
    | SeatsOverCommitmentFinding
    | EscalatorAppliedEarlyFinding,
    Field(discriminator="kind"),
]
finding_adapter: TypeAdapter[Finding] = TypeAdapter(Finding)


# --- Actions ---------------------------------------------------------------------------------


class ActionDraft(LeoModel):
    finding: str
    type: ActionType
    to: str
    subject: str
    body: str
    pre_step: str | None = None
    alternative: ActionType | None = None
    state: ActionState = ActionState.draft
