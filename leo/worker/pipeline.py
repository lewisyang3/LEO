"""The components the worker runs (ROADMAP A4, wired in 2.1).

Tenant-scoped parts are built per job from the job's `Repo`: the vendor resolver, and the
prior terms validation compares with. `Pipeline.stubs()` keeps the A4 stubs for the stages
but resolves vendors in the database, because vendor ids are stored as UUIDs.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from leo.actions.draft import LLMDrafter, TemplateDrafter
from leo.config import Settings, get_settings
from leo.db.read import load_ledger
from leo.db.repo import Repo
from leo.fixtures import FixtureClassifier, load_fixture
from leo.interfaces import (
    Classifier,
    Clock,
    Drafter,
    Extractor,
    Reconciler,
    TransactionSource,
    Validator,
    VendorResolver,
)
from leo.jev import jev_client
from leo.llm import ResponseCache
from leo.match.transactions import CsvTransactionSource
from leo.match.vendors import DbVendorResolver
from leo.pipeline.classify import JevClassifier
from leo.pipeline.extract_llm import LLMExtractor
from leo.pipeline.extract_script import script_extractor
from leo.pipeline.validate import JevPriorComparer, RuleValidator
from leo.reconcile.reconciler import RuleReconciler
from leo.scriptgen.scripts import StoredScripts
from leo.stubs import StubClassifier, StubDrafter, StubExtractor, StubReconciler, StubValidator
from leo.types import EXCLUDED_STATUSES, ExtractedTerm

DEFAULT_TRANSACTIONS = Path("data/synthetic/transactions.csv")
PriorTerms = Callable[[Session, UUID, str, str], list[ExtractedTerm]]


def stored_prior_terms(
    session: Session, tenant_id: UUID, vendor_id: str, path: str
) -> list[ExtractedTerm]:
    """The vendor's stored terms from other documents, with their validated status. A
    `needs_review` prior isn't compared (ROADMAP 2.1). Linked duplicates' terms are already
    left out by `load_ledger`."""
    return [
        t
        for t in load_ledger(session, tenant_id).terms
        if t.vendor == vendor_id and t.document != path and t.status not in EXCLUDED_STATUSES
    ]


@dataclass
class Pipeline:
    classifier: Classifier
    extractors: Sequence[Extractor]  # scripts first, the LLM extractor last
    validator: Validator
    reconciler: Reconciler
    drafter: Drafter
    vendors: Callable[[Repo], VendorResolver] = field(default=DbVendorResolver)
    prior_terms: PriorTerms = field(default=stored_prior_terms)
    transactions: TransactionSource | None = None  # synced at the start of each reconcile
    # The tenant's promoted generated scripts (Phase 4), tried before `extractors`.
    stored_scripts: Callable[[Repo], Extractor | None] | None = None

    @classmethod
    def default(
        cls,
        clock: Clock,
        classifier: str = "jev",
        validator: str = "rules",
        drafter: str = "llm",
        transactions: Path | None = DEFAULT_TRANSACTIONS,
        sender: str = "",
        settings: Settings | None = None,
        builtin_scripts: bool = True,
    ) -> "Pipeline":
        """The real components (2.1). `classifier`: `jev` (live, needs `JEV_API_KEY`) or
        `fixture` (the answer key's classes, by dataset path). `validator`: `rules` (no
        network, the same result every run) or `jev` (adds Jev's prior comparison).
        `drafter`: `llm` (B7, cached under `LEO_LLM_CACHE_DIR`) or `template`. Extraction is
        the tenant's promoted generated scripts (Phase 4), B5's hand-written scripts (left out
        with `builtin_scripts=False`), then B3's LLM extractor (cached)."""
        settings = settings or get_settings()
        cache = ResponseCache(settings.leo_llm_cache_dir)
        clf: Classifier
        if classifier == "jev":
            clf = JevClassifier(jev_client(settings))
        elif classifier == "fixture":
            clf = FixtureClassifier(load_fixture())
        else:
            raise ValueError(f"unknown classifier {classifier!r}")
        if validator == "rules":
            val = RuleValidator(clock)
        elif validator == "jev":
            val = RuleValidator(clock, extra=[JevPriorComparer(jev_client(settings))])
        else:
            raise ValueError(f"unknown validator {validator!r}")
        drf: Drafter
        if drafter == "llm":
            drf = LLMDrafter(cache, settings=settings, sender=sender)
        elif drafter == "template":
            drf = TemplateDrafter(sender=sender)
        else:
            raise ValueError(f"unknown drafter {drafter!r}")
        return cls(
            classifier=clf,
            extractors=[
                *([script_extractor()] if builtin_scripts else []),
                LLMExtractor(clock, cache, settings=settings),
            ],
            validator=val,
            reconciler=RuleReconciler(),
            drafter=drf,
            transactions=CsvTransactionSource(transactions) if transactions else None,
            stored_scripts=StoredScripts(clock),
        )

    @classmethod
    def stubs(cls) -> "Pipeline":
        return cls(
            classifier=StubClassifier(),
            extractors=[StubExtractor()],
            validator=StubValidator(),
            reconciler=StubReconciler(),
            drafter=StubDrafter(),
        )
