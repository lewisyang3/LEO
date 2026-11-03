"""Runnable placeholders for the interfaces in `leo.interfaces` (ROADMAP 0.5).

They return valid, empty results so the worker (A4) runs end to end before Track B and
Track C plug in. 0.6 adds fixture-backed versions.
"""

import datetime
import re
from collections.abc import Sequence

from leo.interfaces import (
    Classification,
    DocumentText,
    Extraction,
    Ledger,
    Reconciliation,
    Validated,
)
from leo.types import (
    ActionDraft,
    ActionType,
    DocumentClass,
    ExtractedTerm,
    Finding,
    Transaction,
)

STUB = "stub"


class StubClassifier:
    def __init__(self, default: DocumentClass = DocumentClass.contract) -> None:
        self.default = default

    def classify(self, doc: DocumentText) -> Classification:
        return Classification(doc_class=self.default, confidence=0.0, engine=STUB)


class StubExtractor:
    def __init__(self, vendor_name: str | None = None) -> None:
        self.vendor_name = vendor_name

    def extract(self, doc: DocumentText) -> Extraction | None:
        return Extraction(vendor_name=self.vendor_name, engine=STUB)


class StubValidator:
    def validate(
        self, doc: DocumentText, extraction: Extraction, prior_terms: Sequence[ExtractedTerm]
    ) -> Validated:
        return Validated(
            extraction=extraction, engine=STUB, vendor_ok=extraction.vendor_name is not None
        )


class StaticTransactionSource:
    def __init__(self, transactions: Sequence[Transaction] = ()) -> None:
        self._transactions = list(transactions)

    def transactions(self) -> list[Transaction]:
        return list(self._transactions)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


class StubVendorResolver:
    """Slugs the input. The same input always gives the same id."""

    def __init__(self) -> None:
        self.vendors: set[str] = set()

    def find_vendor(self, name: str) -> str | None:
        slug = _slug(name)
        return slug if slug in self.vendors else None

    def vendor_for_name(self, name: str) -> str:
        self.vendors.add(slug := _slug(name))
        return slug

    def vendor_for_descriptor(self, descriptor: str) -> str:
        return self.vendor_for_name(descriptor)


class StubReconciler:
    def reconcile(self, ledger: Ledger, today: datetime.date) -> Reconciliation:
        return Reconciliation()


class StubDrafter:
    def draft(self, finding: Finding, ledger: Ledger) -> ActionDraft:
        vendor = next((v for v in ledger.vendors if v.id == finding.vendor), None)
        to = vendor.contacts[0].email if vendor and vendor.contacts else ""
        return ActionDraft(
            finding=finding.id,
            type=ActionType.billing_dispute,
            to=to,
            subject=finding.title,
            body="(stub draft)",
        )
