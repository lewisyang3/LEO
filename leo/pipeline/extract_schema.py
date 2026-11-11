"""The structured output the extraction LLM fills.

Kept separate from the shared types in `leo.types`: the model fills only what a document says.
`leo.pipeline.extract_llm` maps these models to `ExtractedTerm`, `InvoiceFact` and `Contact`. That
step sets ids, `document`, `vendor` and status, and computes the dates that need "today".

Rules for these models:
- No defaults. Every field is required, and it is `null` when the document doesn't say.
  Structured outputs need every property listed in `required`.
- `quotes` come first, so the model anchors on the text before it fills the values.
- Money and percentages are `float` here, and become `Decimal` in the mapper.
- Helper fields (`term_start`, `initial_term_months`, `anniversary_base_date`) are the inputs
  to date arithmetic the mapper does. They are not part of the shared value shapes.
"""

import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from leo.types import AppliesTo, PricePer, RelativeTo, SeatUnit


class LLMModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Values ----------------------------------------------------------------------------------


class LLMPrice(LLMModel):
    amount: float | None
    currency: str | None
    per: PricePer | None


class LLMPriceCap(LLMModel):
    max_increase_pct: float | None
    applies_to: AppliesTo | None
    baseline: str | None


class LLMEscalator(LLMModel):
    pct: float | None
    schedule: str | None
    first_application: datetime.date | None = Field(
        description="Only a date the document states outright; otherwise null."
    )
    anniversary_base_date: datetime.date | None = Field(
        description="When the increase happens on each anniversary of a named date (effective "
        "date, service start date), that date. Otherwise null."
    )


class LLMRenewal(LLMModel):
    auto_renew: bool | None
    initial_term_end: datetime.date | None = Field(
        description="Only an end date the document states outright; otherwise null."
    )
    term_start: datetime.date | None = Field(
        description="The date the initial term starts (effective date or service start date)."
    )
    initial_term_months: int | None = Field(
        description="The initial term's length in months, when stated as a length."
    )
    renewal_length_months: int | None
    renewal_pricing: str | None
    early_termination_fee: str | None


class LLMNoticeWindow(LLMModel):
    days: int | None
    relative_to: RelativeTo | None
    applies_to: AppliesTo | None
    deliver_to: str | None


class LLMSeatCount(LLMModel):
    included: int | None
    unit: SeatUnit | None
    overage_per_unit_month: float | None
    increase_requires: str | None


class LLMDisputeWindow(LLMModel):
    days: int | None
    relative_to: RelativeTo | None
    applies_to: AppliesTo | None
    deliver_to: str | None


# --- Terms -----------------------------------------------------------------------------------


class _LLMTermBase(LLMModel):
    quotes: list[str] = Field(
        description="Exact, contiguous text copied from the document. The most specific clause "
        "comes first."
    )
    plan_label: str | None
    agreement_ref: str | None
    effective_date: datetime.date | None


# Each type's item, as the model writes it: no `type` field, because each type has its own
# list in `LLMExtraction`. One union list of all seven types compiles to a grammar the API
# rejects as too large.


class LLMPriceItem(_LLMTermBase):
    value: LLMPrice
    old_value: LLMPrice | None


class LLMPriceCapItem(_LLMTermBase):
    value: LLMPriceCap


class LLMEscalatorItem(_LLMTermBase):
    value: LLMEscalator


class LLMRenewalItem(_LLMTermBase):
    value: LLMRenewal


class LLMNoticeWindowItem(_LLMTermBase):
    value: LLMNoticeWindow


class LLMSeatCountItem(_LLMTermBase):
    value: LLMSeatCount


class LLMDisputeWindowItem(_LLMTermBase):
    value: LLMDisputeWindow


# The same items with their type, which the mapper validates against.


class LLMPriceTerm(LLMPriceItem):
    type: Literal["price"]


class LLMPriceCapTerm(LLMPriceCapItem):
    type: Literal["price_cap"]


class LLMEscalatorTerm(LLMEscalatorItem):
    type: Literal["escalator"]


class LLMRenewalTerm(LLMRenewalItem):
    type: Literal["renewal"]


class LLMNoticeWindowTerm(LLMNoticeWindowItem):
    type: Literal["notice_window"]


class LLMSeatCountTerm(LLMSeatCountItem):
    type: Literal["seat_count"]


class LLMDisputeWindowTerm(LLMDisputeWindowItem):
    type: Literal["dispute_window"]


LLMTerm = Annotated[
    LLMPriceTerm
    | LLMPriceCapTerm
    | LLMEscalatorTerm
    | LLMRenewalTerm
    | LLMNoticeWindowTerm
    | LLMSeatCountTerm
    | LLMDisputeWindowTerm,
    Field(discriminator="type"),
]


# Output list name -> term type.
TERM_LISTS = {
    "prices": "price",
    "price_caps": "price_cap",
    "escalators": "escalator",
    "renewals": "renewal",
    "notice_windows": "notice_window",
    "seat_counts": "seat_count",
    "dispute_windows": "dispute_window",
}


# --- Invoice facts and contacts --------------------------------------------------------------


class LLMInvoiceFact(LLMModel):
    quotes: list[str]
    invoice_number: str | None
    billing_period: str | None
    invoice_date: datetime.date | None
    amount: float | None
    seats_billed: int | None


class LLMContact(LLMModel):
    email: str
    purpose: str
    quote: str


# --- The three parts ------------------------------------------------------------------------
#
# The API compiles a structured-output schema into a grammar, and caps it: at most 16 nullable
# fields, at most 24 optional ones, and a total size. All ten lists in one schema are far over,
# so each document gets three requests, one per part, and the extractor merges the answers.
# Each part below was checked against the API on 2026-11-04. Adding fields to a part can push
# it over the cap (400 "compiled grammar is too large"): re-check, and split further if needed.


class LLMPricesRenewals(LLMModel):
    vendor_name: str | None
    prices: list[LLMPriceItem]
    renewals: list[LLMRenewalItem]


class LLMCapsEscalatorsSeats(LLMModel):
    vendor_name: str | None
    price_caps: list[LLMPriceCapItem]
    escalators: list[LLMEscalatorItem]
    seat_counts: list[LLMSeatCountItem]


class LLMWindowsInvoicesContacts(LLMModel):
    vendor_name: str | None
    notice_windows: list[LLMNoticeWindowItem]
    dispute_windows: list[LLMDisputeWindowItem]
    invoice_facts: list[LLMInvoiceFact]
    contacts: list[LLMContact]


# Part name -> (output model, what the request asks for). Order is the merge order.
EXTRACTION_PARTS: dict[str, tuple[type[LLMModel], str]] = {
    "prices_renewals": (LLMPricesRenewals, "price terms and renewal terms"),
    "caps_escalators_seats": (
        LLMCapsEscalatorsSeats,
        "price_cap terms, escalator terms and seat_count terms",
    ),
    "windows_invoices_contacts": (
        LLMWindowsInvoicesContacts,
        "notice_window terms, dispute_window terms, invoice facts and contacts",
    ),
}


# --- Few-shot corrections (Phase 4, feedback loop) --------------------------------------------
#
# A reviewer's correction is shown to the model in the part that extracts that kind of item,
# and only for fields the model itself writes. Dates the mapper computes afterwards
# (`current_term_end`, `next_deadline`) aren't in the model's output, and `first_application` /
# `initial_term_end` are filled only when the document states them outright, so a correction of
# any of them would push the model to write what the prompt says to leave null.

INVOICE_FACT = "invoice_fact"
_DERIVED = frozenset({"value.first_application", "value.initial_term_end"})
_TERM_FIELDS = ("plan_label", "agreement_ref", "effective_date")


def _items() -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    part_of: dict[str, str] = {}
    fields: dict[str, frozenset[str]] = {}
    for part, (model, _) in EXTRACTION_PARTS.items():
        for name, info in model.model_fields.items():
            if name in TERM_LISTS:
                item = info.annotation.__args__[0]  # list[LLM...Item]
                value = item.model_fields["value"].annotation
                keys = {f"value.{k}" for k in value.model_fields} - _DERIVED
                part_of[TERM_LISTS[name]] = part
                fields[TERM_LISTS[name]] = frozenset(keys | set(_TERM_FIELDS))
            elif name == "invoice_facts":
                part_of[INVOICE_FACT] = part
                fields[INVOICE_FACT] = frozenset(LLMInvoiceFact.model_fields) - {"quotes"}
    return part_of, fields


# Item kind (a term type, or "invoice_fact") -> the part that extracts it, and the fields of it
# a correction may show the model.
PART_OF_ITEM, EXAMPLE_FIELDS = _items()
