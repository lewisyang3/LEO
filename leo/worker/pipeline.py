"""The components the worker runs (ROADMAP A4). Stubs now; 2.1 swaps in Track B and C's."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from leo.interfaces import Classifier, Drafter, Extractor, Reconciler, Validator, VendorResolver
from leo.stubs import (
    StubClassifier,
    StubDrafter,
    StubExtractor,
    StubReconciler,
    StubValidator,
    StubVendorResolver,
)
from leo.types import ExtractedTerm


def no_prior_terms(vendor_id: str) -> list[ExtractedTerm]:
    """A4 has no stored terms to compare with. 2.1 reads the vendor's terms from the store."""
    return []


@dataclass
class Pipeline:
    classifier: Classifier
    extractors: Sequence[Extractor]  # scripts first, the LLM extractor last
    validator: Validator
    vendors: VendorResolver
    reconciler: Reconciler
    drafter: Drafter
    prior_terms: Callable[[str], list[ExtractedTerm]] = field(default=no_prior_terms)

    @classmethod
    def stubs(cls) -> "Pipeline":
        return cls(
            classifier=StubClassifier(),
            extractors=[StubExtractor()],
            validator=StubValidator(),
            vendors=StubVendorResolver(),
            reconciler=StubReconciler(),
            drafter=StubDrafter(),
        )
