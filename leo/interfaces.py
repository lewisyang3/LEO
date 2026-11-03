"""Interfaces between tracks (ROADMAP 0.5).

Each pipeline stage is a `Protocol`, with the models it takes and returns. Track A's worker
calls these. Track B and Track C implement them. Until they do, `leo.stubs` stands in.

Any change to this module needs all three teammates to agree (ROADMAP 0.7).

Contracts on extraction output:
- `document` on every term, invoice fact and contact is `DocumentText.document.path`.
- Before vendor resolution, `vendor` on terms and invoice facts holds `Extraction.vendor_name`
  as written in the document (for a forward, from the forwarded header block). The worker
  resolves it with `VendorResolver.vendor_for_name` and calls `Extraction.with_vendor`.
- When `vendor_name` is None, the worker skips resolution and validates with `prior_terms=[]`.
  Terms and facts then carry `vendor=""`, and the Validator flags each one `vendor_missing`.
- Ids are provisional, unique within one extraction. The repository (A2) replaces them with
  natural keys on write. Nothing downstream may parse an id.
- Quote offsets, citation rows and span keys are computed by the repository (A2) from the stored
  text, with `leo.rules.locate_quote` and `leo.rules.span_key`. The Validator only reports
  `quote_not_found`.

Contracts on `Ledger`:
- `terms` holds every status. The reconciler chooses which to use (F2 needs the superseded
  old price).
- `terms` and `invoice_facts` leave out items from a linked duplicate (D2). `documents` lists
  every document with its duplicate links, so the reconciler can fill `linked_duplicates`.
- `transaction_plan_map` has entries only where a vendor has more than one plan.
"""

import datetime
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from leo.types import (
    ActionDraft,
    Contact,
    DocumentClass,
    ExtractedTerm,
    Finding,
    InvoiceFact,
    LeoModel,
    SourceDocument,
    Transaction,
    Vendor,
)

# --- Stage I/O models ------------------------------------------------------------------------


class DocumentText(LeoModel):
    """A document and its raw stored text (`leo.rules.read_source_text`, headers included)."""

    document: SourceDocument  # doc_class is None before classify, set after
    text: str


class Classification(LeoModel):
    doc_class: DocumentClass
    confidence: float = Field(ge=0, le=1)
    engine: str  # recorded as processed_by: "jev", "llm:claude-haiku-5-5", "stub"
    reason: str | None = None


class Extraction(LeoModel):
    vendor_name: str | None
    terms: list[ExtractedTerm] = []
    invoice_facts: list[InvoiceFact] = []
    contacts: list[Contact] = []
    engine: str  # recorded as processed_by: "script:<name>@<version>", "llm:<model>", "stub"
    raw_output: dict[str, Any] | None = None  # stored as JSONB

    def with_vendor(self, vendor_id: str) -> "Extraction":
        """A copy with `vendor` set on every term and invoice fact."""
        return self.model_copy(
            update={
                "terms": [t.model_copy(update={"vendor": vendor_id}) for t in self.terms],
                "invoice_facts": [
                    f.model_copy(update={"vendor": vendor_id}) for f in self.invoice_facts
                ],
            }
        )


class ValidationIssue(LeoModel):
    item: str  # term or invoice-fact id
    check: str  # quote_not_found, date_unparsed, amount_implausible, conflicts_prior, ...
    message: str


class Validated(LeoModel):
    """The extraction with failing items set to `needs_review` (never dropped)."""

    extraction: Extraction
    issues: list[ValidationIssue] = []
    engine: str


class Ledger(LeoModel):
    """Everything the reconciler and drafter read. Map names match the answer key."""

    vendors: list[Vendor]
    documents: list[SourceDocument]
    terms: list[ExtractedTerm]
    invoice_facts: list[InvoiceFact]
    transactions: list[Transaction]
    transaction_vendor_map: dict[str, str]  # transaction id -> vendor id
    transaction_plan_map: dict[str, str]  # transaction id -> plan_label


class Supersession(LeoModel):
    old: str  # term id
    new: str  # term id


class Reconciliation(LeoModel):
    findings: list[Finding] = []
    supersessions: list[Supersession] = []


# --- Protocols -------------------------------------------------------------------------------


@runtime_checkable
class Classifier(Protocol):
    """Stage 1. Jev, with the Haiku investigate step for unsure items (B2)."""

    def classify(self, doc: DocumentText) -> Classification: ...


@runtime_checkable
class Extractor(Protocol):
    """Stages 2 and 3. The worker tries script extractors first, then the LLM extractor."""

    def extract(self, doc: DocumentText) -> Extraction | None:
        """None means "not mine" (a script with no matching template). The LLM extractor
        always returns an Extraction."""
        ...


@runtime_checkable
class Validator(Protocol):
    """Stage 4 (B4)."""

    def validate(
        self, doc: DocumentText, extraction: Extraction, prior_terms: Sequence[ExtractedTerm]
    ) -> Validated:
        """`prior_terms` are the resolved vendor's stored terms, for Jev's comparison."""
        ...


@runtime_checkable
class TransactionSource(Protocol):
    """Card and bill-pay charges: `transactions.csv` now, Ramp later (A5)."""

    def transactions(self) -> list[Transaction]: ...


@runtime_checkable
class VendorResolver(Protocol):
    """Both methods get or create a vendor and return its id. Aliases grow with matches (A5)."""

    def vendor_for_name(self, name: str) -> str: ...

    def vendor_for_descriptor(self, descriptor: str) -> str: ...


@runtime_checkable
class Reconciler(Protocol):
    """Stage 5 rules (C1). Pure: the caller supplies `today` from a `Clock`."""

    def reconcile(self, ledger: Ledger, today: datetime.date) -> Reconciliation: ...


@runtime_checkable
class Drafter(Protocol):
    """Stage 5 drafting (B7). The ledger supplies cited terms, invoice facts and contacts."""

    def draft(self, finding: Finding, ledger: Ledger) -> ActionDraft: ...


@runtime_checkable
class Clock(Protocol):
    """The only source of "today". See `leo.clock.get_clock`."""

    def today(self) -> datetime.date: ...
