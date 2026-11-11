"""The answer key as a fixture.

`load_fixture()` reads `data/synthetic/expected.json`, `transactions.csv` and the source
documents into the shared types with no field mapping. The key's answer-only fields
(`expected_action`, `days_until_deadline`, `why_planted`) are split off each finding into
`ExpectedFinding`, so `Finding` stays the shared type.

The `Fixture*` stages implement the interfaces in `leo.interfaces` by returning the key's
answers, so the worker and the UI run on realistic data without the real stages. They record
`engine="fixture"`. Use `StaticTransactionSource(fixture.transactions)` for transactions.
"""

import csv
import datetime
import hashlib
import json
from pathlib import Path

from leo.clock import FixedClock
from leo.interfaces import (
    Classification,
    DocumentText,
    Extraction,
    Ledger,
    Reconciliation,
    Supersession,
)
from leo.rules import read_source_text
from leo.stubs import StubVendorResolver
from leo.types import (
    ActionDraft,
    ActionType,
    Contact,
    ExtractedTerm,
    Finding,
    InvoiceFact,
    LeoModel,
    Money,
    SourceDocument,
    Transaction,
    Vendor,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic"
FIXTURE = "fixture"


# --- Answer-key-only models ------------------------------------------------------------------


class ExpectedAction(LeoModel):
    type: ActionType
    to: str
    alternative: ActionType | None = None
    pre_step: str | None = None
    must_include: list[str]


class ExpectedFinding(LeoModel):
    finding: Finding
    expected_action: ExpectedAction
    days_until_deadline: int | None = None  # absent for F4, which has no deadline
    why_planted: str


class NonFinding(LeoModel):
    id: str
    vendor: str | None  # None for ZapLedger (N5), which is not a vendor
    why: str
    terms: list[str] = []
    transactions: list[str] = []
    documents: list[str] = []


class DedupCheck(LeoModel):
    id: str
    kind: str
    expected: str
    vendor: str | None = None
    documents: list[str] = []


class NotVendor(LeoModel):
    name: str
    why: str


class Totals(LeoModel):
    overcharged_to_date: Money
    annualized_leak_excluding_renewal_decision: Money
    renewal_decision_at_stake: Money
    notes: str


class CustomerUser(LeoModel):
    name: str
    role: str
    email: str


class Customer(LeoModel):
    name: str
    description: str
    users: list[CustomerUser]


# --- The fixture -----------------------------------------------------------------------------


class Fixture(LeoModel):
    root: Path
    schema_version: int
    dataset: str
    demo_today: datetime.date
    lookahead_days: int
    customer: Customer
    vendors: list[Vendor]
    not_vendors: list[NotVendor]
    documents: list[SourceDocument]
    terms: list[ExtractedTerm]
    invoice_facts: list[InvoiceFact]
    transactions: list[Transaction]
    transaction_vendor_map: dict[str, str]
    transaction_plan_map: dict[str, str]
    expected_findings: list[ExpectedFinding]
    non_findings: list[NonFinding]
    dedup_checks: list[DedupCheck]
    totals: Totals

    @property
    def findings(self) -> list[Finding]:
        return [x.finding for x in self.expected_findings]

    def ledger(self) -> Ledger:
        return Ledger(
            vendors=self.vendors,
            documents=self.documents,
            terms=self.terms,
            invoice_facts=self.invoice_facts,
            transactions=self.transactions,
            transaction_vendor_map=self.transaction_vendor_map,
            transaction_plan_map=self.transaction_plan_map,
        ).model_copy(deep=True)

    def clock(self) -> FixedClock:
        return FixedClock(self.demo_today)

    def vendor(self, vendor_id: str) -> Vendor:
        return next(v for v in self.vendors if v.id == vendor_id)

    def document(self, path: str) -> SourceDocument:
        return next(d for d in self.documents if d.path == path)

    def document_text(self, path: str, classified: bool = False) -> DocumentText:
        """Only what a stage may see: the path, hash and text, plus `doc_class` when
        `classified`. The key's answers stay in `fixture.documents`."""
        key_doc = self.document(path)
        doc = SourceDocument(
            path=path,
            doc_class=key_doc.doc_class if classified else None,
            vendor=None,
            sha256=key_doc.sha256,
        )
        return DocumentText(document=doc, text=read_source_text(self.root / path))

    def terms_for(self, path: str) -> list[ExtractedTerm]:
        return [x for x in self.terms if x.document == path]

    def invoice_facts_for(self, path: str) -> list[InvoiceFact]:
        return [x for x in self.invoice_facts if x.document == path]

    def contacts_for(self, path: str) -> list[Contact]:
        return [c for v in self.vendors for c in v.contacts if c.document == path]


_KEY_ONLY_FINDING_FIELDS = ("expected_action", "days_until_deadline", "why_planted")


def load_fixture(root: Path = DATA_DIR) -> Fixture:
    """A fresh copy each call: the models are mutable, so callers don't share state."""
    key = json.loads((root / "expected.json").read_text())
    with (root / "transactions.csv").open(newline="") as f:
        transactions = list(csv.DictReader(f))
    documents = [
        d | {"sha256": hashlib.sha256((root / d["path"]).read_bytes()).hexdigest()}
        for d in key["documents"]
    ]
    expected_findings = []
    for raw in key["findings"]:
        finding = {k: v for k, v in raw.items() if k not in _KEY_ONLY_FINDING_FIELDS}
        extra = {k: raw[k] for k in _KEY_ONLY_FINDING_FIELDS if k in raw}
        expected_findings.append({"finding": finding} | extra)
    return Fixture.model_validate(
        {k: v for k, v in key.items() if k != "findings"}
        | {
            "root": root,
            "documents": documents,
            "transactions": transactions,
            "expected_findings": expected_findings,
        }
    )


# --- Fixture-backed stages -------------------------------------------------------------------


class FixtureClassifier:
    def __init__(self, fixture: Fixture) -> None:
        self.fixture = fixture

    def classify(self, doc: DocumentText) -> Classification:
        doc_class = self.fixture.document(doc.document.path).doc_class
        assert doc_class is not None
        return Classification(doc_class=doc_class, confidence=1.0, engine=FIXTURE)


class FixtureExtractor:
    """The key's items for the document, with `vendor` set to the vendor's name as written
    (its `canonical_name`), as before vendor resolution. The forward (02) has no items: the
    key records its terms from 01 only (D2)."""

    def __init__(self, fixture: Fixture) -> None:
        self.fixture = fixture

    def extract(self, doc: DocumentText) -> Extraction | None:
        path = doc.document.path
        vendor_id = self.fixture.document(path).vendor
        if vendor_id is None:
            return Extraction(vendor_name=None, engine=FIXTURE)
        name = self.fixture.vendor(vendor_id).canonical_name
        return Extraction(
            vendor_name=name,
            terms=[
                x.model_copy(update={"vendor": name}, deep=True)
                for x in self.fixture.terms_for(path)
            ],
            invoice_facts=[
                x.model_copy(update={"vendor": name}, deep=True)
                for x in self.fixture.invoice_facts_for(path)
            ],
            contacts=[c.model_copy(deep=True) for c in self.fixture.contacts_for(path)],
            engine=FIXTURE,
        )


class FixtureVendorResolver:
    """Case-insensitive lookup over the key's canonical names and aliases. Unknown names go to
    a `StubVendorResolver`, which gets or creates and remembers them."""

    def __init__(self, fixture: Fixture) -> None:
        self.names = {
            n.lower(): v.id for v in fixture.vendors for n in (v.canonical_name, *v.aliases)
        }
        self.fallback = StubVendorResolver()

    def find_vendor(self, name: str) -> str | None:
        return self.names.get(name.lower()) or self.fallback.find_vendor(name)

    def vendor_for_name(self, name: str) -> str:
        return self.names.get(name.lower()) or self.fallback.vendor_for_name(name)

    def vendor_for_descriptor(self, descriptor: str) -> str:
        return self.names.get(descriptor.lower()) or self.fallback.vendor_for_descriptor(descriptor)


class FixtureReconciler:
    """The key's findings and supersessions, whatever the ledger and date."""

    def __init__(self, fixture: Fixture) -> None:
        self.fixture = fixture

    def reconcile(self, ledger: Ledger, today: datetime.date) -> Reconciliation:
        return Reconciliation(
            findings=[f.model_copy(deep=True) for f in self.fixture.findings],
            supersessions=[
                Supersession(old=x.id, new=x.superseded_by)
                for x in self.fixture.terms
                if x.superseded_by is not None
            ],
        )


class FixtureDrafter:
    """A draft from the key's `expected_action`. The body lists its `must_include` strings."""

    def __init__(self, fixture: Fixture) -> None:
        self.expected = {x.finding.id: x.expected_action for x in fixture.expected_findings}

    def draft(self, finding: Finding, ledger: Ledger) -> ActionDraft:
        action = self.expected[finding.id]
        return ActionDraft(
            finding=finding.id,
            type=action.type,
            to=action.to,
            subject=finding.title,
            body="\n".join(action.must_include),
            pre_step=action.pre_step,
            alternative=action.alternative,
        )
