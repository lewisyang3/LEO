"""Scoring stage output against the answer key (ROADMAP B1). Pure functions, no I/O.

Classification and extraction are scored independently, so a classifier mistake doesn't hide
extractor quality.

Extraction rules:
- A document's expected items are the key's terms, invoice facts and contacts for it. A
  duplicate document (`probable_duplicate_of` or `exact_duplicate_of`) is scored against its
  original's items: the extractor reads what is in the document, and linking duplicates is A6's
  job. The key's quotes appear verbatim in both duplicates.
- Predicted and key items are paired greedily by agreement within one document: terms within
  one term type, invoice facts within the document, contacts on email. Ids are never compared,
  because extraction ids are provisional (`leo.interfaces`).
- A term or invoice fact pairs only with a key item it agrees with: the first quotes' spans
  overlap, or a strict majority of the informative fields agree (the key value isn't None, and
  `vendor` doesn't count). An invented item is then both an extra and a miss, not a match with
  wrong fields.
- A finding is covered only when every term and invoice fact it cites was matched in its own
  document with every field right.
- Quotes are located with `leo.rules.locate_quote` in the scored document's raw text.
"""

import datetime
from collections import Counter
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from leo.fixtures import Fixture, FixtureVendorResolver
from leo.interfaces import Classification, Extraction, ValidationIssue
from leo.rules import locate_quote
from leo.types import (
    Contact,
    DocumentClass,
    ExtractedTerm,
    InvoiceFact,
    LeoModel,
    SourceDocument,
)

TERMS = "terms"
INVOICE_FACTS = "invoice_facts"
CONTACTS = "contacts"
KINDS = (TERMS, INVOICE_FACTS, CONTACTS)

# --- Result models ---------------------------------------------------------------------------


class Rate(LeoModel):
    hit: int = 0
    total: int = 0

    def rate(self) -> float | None:
        return self.hit / self.total if self.total else None

    def add(self, ok: bool) -> None:
        self.hit += ok
        self.total += 1


class PrecisionRecall(LeoModel):
    matched: int = 0
    expected: int = 0
    predicted: int = 0

    def precision(self) -> float | None:
        return self.matched / self.predicted if self.predicted else None

    def recall(self) -> float | None:
        return self.matched / self.expected if self.expected else None


class FieldResult(LeoModel):
    field: str  # "vendor", "value.amount", "old_value.amount", ...
    expected: str | None
    predicted: str | None
    ok: bool


class ItemMatch(LeoModel):
    key_id: str  # term or invoice-fact id; the email for a contact
    pred_id: str
    type: str  # term type, "invoice_fact" or "contact"
    fields: list[FieldResult]
    quote_found: bool  # every predicted quote is in the document
    quote_matches_key: bool  # the first quotes' spans overlap


class MissedItem(LeoModel):
    key_id: str
    type: str


class ExtraItem(LeoModel):
    pred_id: str
    type: str
    first_quote: str | None
    quote_found: bool


class ItemScore(LeoModel):
    matched: list[ItemMatch] = []
    missed: list[MissedItem] = []
    extra: list[ExtraItem] = []


class DocClassification(LeoModel):
    path: str
    expected: DocumentClass
    predicted: DocumentClass | None  # None when the engine raised
    confidence: float | None
    matters: bool  # cited by a planted finding (F1-F5)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.predicted == self.expected


class ClassificationSummary(LeoModel):
    accuracy: Rate
    matters_recall: Rate  # documents that matter predicted non-noise
    noise_false_pass: Rate  # key-noise documents predicted non-noise
    mean_confidence: float | None
    confusion: dict[str, dict[str, int]]  # key class -> predicted class ("error") -> count


class DocValidation(LeoModel):
    """The validator's result for one document (B4). `needs_review` holds predicted ids."""

    engine: str | None = None
    vendor_ok: bool = False
    issues: list[ValidationIssue] = []
    needs_review: list[str] = []
    error: str | None = None  # the validator raised; the extraction is scored unvalidated


class DocExtraction(LeoModel):
    path: str
    duplicate_of: str | None
    terms: ItemScore
    invoice_facts: ItemScore
    contacts: ItemScore
    error: str | None = None
    validation: DocValidation | None = None  # None when no validator ran

    def kind(self, kind: str) -> ItemScore:
        return getattr(self, kind)


class KindSummary(LeoModel):
    overall: PrecisionRecall
    by_type: dict[str, PrecisionRecall] = {}
    fields: dict[str, Rate] = {}
    quote_found: Rate  # over every predicted item, matched or not
    quote_matches_key: Rate  # over matched items


class ExtractionSummary(LeoModel):
    terms: KindSummary
    invoice_facts: KindSummary
    contacts: KindSummary
    # finding id -> key term and invoice-fact ids it cites that were not matched in their own
    # document with every field right, or whose match was set to needs_review by the
    # validator. An empty list means the finding is covered.
    finding_coverage: dict[str, list[str]]


# --- Comparison helpers ----------------------------------------------------------------------


def _norm(v: Any) -> Any:
    """Numbers as Decimal, strings stripped, whitespace-collapsed and case-folded."""
    if v is None or isinstance(v, bool | datetime.date):
        return v
    if isinstance(v, int | float | Decimal):
        return Decimal(str(v)).normalize()
    if isinstance(v, str):
        return " ".join(v.split()).casefold()
    return v


def _show(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    return str(v)


def _field(name: str, expected: Any, predicted: Any) -> FieldResult:
    return FieldResult(
        field=name,
        expected=_show(expected),
        predicted=_show(predicted),
        ok=_norm(expected) == _norm(predicted),
    )


def _model_fields(prefix: str, key: BaseModel, pred: BaseModel | None) -> list[FieldResult]:
    pred_values = pred.model_dump() if pred is not None else {}
    return [_field(f"{prefix}.{k}", v, pred_values.get(k)) for k, v in key.model_dump().items()]


def _locate(text: str, quote: str) -> tuple[int, int] | None:
    """`locate_quote`, except that an empty or blank quote is never found."""
    return locate_quote(text, quote) if quote.strip() else None


def _first_span(text: str, quotes: Sequence[str]) -> tuple[int, int] | None:
    return _locate(text, quotes[0]) if quotes else None


def _overlaps(a: tuple[int, int] | None, b: tuple[int, int] | None) -> bool:
    return a is not None and b is not None and a[0] < b[1] and b[0] < a[1]


def _all_found(text: str, quotes: Sequence[str]) -> bool:
    return bool(quotes) and all(_locate(text, q) is not None for q in quotes)


def _agrees(fields: Sequence[FieldResult]) -> bool:
    """A strict majority of the informative fields are ok: the key value isn't None, and
    `vendor` doesn't count (it is nearly always right, so it doesn't identify the item)."""
    informative = [f for f in fields if f.field != "vendor" and f.expected is not None]
    return 2 * sum(f.ok for f in informative) > len(informative)


class _Item:
    """One side of a pairing: an id, a type label, its quotes and the object itself."""

    def __init__(self, id: str, type: str, quotes: Sequence[str], obj: Any) -> None:
        self.id, self.type, self.quotes, self.obj = id, type, list(quotes), obj


def _pair(
    text: str,
    preds: list[_Item],
    keys: list[_Item],
    group: Callable[[_Item], Any],
    fields: Callable[[_Item, _Item], list[FieldResult]],
    require_agreement: bool = True,
) -> ItemScore:
    """Greedy pairing within groups by agreement: matching fields, +2 when the first quotes'
    spans overlap. Ties go to the earlier key item, then the earlier prediction. With
    `require_agreement`, a pair is a candidate only if the quotes overlap or `_agrees`."""
    candidates = []
    for pi, p in enumerate(preds):
        for ki, k in enumerate(keys):
            if group(p) != group(k):
                continue
            fs = fields(k, p)
            overlap = _overlaps(_first_span(text, p.quotes), _first_span(text, k.quotes))
            if require_agreement and not (overlap or _agrees(fs)):
                continue
            score = sum(f.ok for f in fs) + 2 * overlap
            candidates.append((-score, ki, pi, fs, overlap))
    candidates.sort(key=lambda c: c[:3])

    used_p: set[int] = set()
    used_k: set[int] = set()
    out = ItemScore()
    for _, ki, pi, fs, overlap in candidates:
        if ki in used_k or pi in used_p:
            continue
        used_k.add(ki)
        used_p.add(pi)
        out.matched.append(
            ItemMatch(
                key_id=keys[ki].id,
                pred_id=preds[pi].id,
                type=keys[ki].type,
                fields=fs,
                quote_found=_all_found(text, preds[pi].quotes),
                quote_matches_key=overlap,
            )
        )
    out.matched.sort(key=lambda m: [k.id for k in keys].index(m.key_id))
    out.missed = [
        MissedItem(key_id=k.id, type=k.type) for i, k in enumerate(keys) if i not in used_k
    ]
    out.extra = [
        ExtraItem(
            pred_id=p.id,
            type=p.type,
            first_quote=p.quotes[0] if p.quotes else None,
            quote_found=_all_found(text, p.quotes),
        )
        for i, p in enumerate(preds)
        if i not in used_p
    ]
    return out


# --- Classification --------------------------------------------------------------------------


def documents_that_matter(fixture: Fixture) -> set[str]:
    return {path for f in fixture.findings for path in f.documents}


def score_classification(
    fixture: Fixture,
    path: str,
    result: Classification | None,
    error: str | None = None,
) -> DocClassification:
    expected = fixture.document(path).doc_class
    assert expected is not None
    return DocClassification(
        path=path,
        expected=expected,
        predicted=result.doc_class if result else None,
        confidence=result.confidence if result else None,
        matters=path in documents_that_matter(fixture),
        error=error,
    )


def summarize_classification(docs: Sequence[DocClassification]) -> ClassificationSummary:
    accuracy, matters, noise = Rate(), Rate(), Rate()
    confusion: dict[str, Counter[str]] = {}
    for d in docs:
        accuracy.add(d.ok)
        passed = d.predicted is not None and d.predicted != DocumentClass.noise
        if d.matters:
            matters.add(passed)
        if d.expected == DocumentClass.noise:
            noise.add(passed)
        predicted = d.predicted.value if d.predicted else "error"
        confusion.setdefault(d.expected.value, Counter())[predicted] += 1
    confidences = [d.confidence for d in docs if d.confidence is not None]
    return ClassificationSummary(
        accuracy=accuracy,
        matters_recall=matters,
        noise_false_pass=noise,
        mean_confidence=sum(confidences) / len(confidences) if confidences else None,
        confusion={k: dict(v) for k, v in confusion.items()},
    )


# --- Extraction ------------------------------------------------------------------------------


def duplicate_of(doc: SourceDocument) -> str | None:
    return doc.probable_duplicate_of or doc.exact_duplicate_of


def expected_items(
    fixture: Fixture, path: str
) -> tuple[list[ExtractedTerm], list[InvoiceFact], list[Contact]]:
    source = duplicate_of(fixture.document(path)) or path
    return (
        fixture.terms_for(source),
        fixture.invoice_facts_for(source),
        fixture.contacts_for(source),
    )


def score_extraction(
    fixture: Fixture,
    path: str,
    text: str,
    extraction: Extraction | None,
    error: str | None = None,
) -> DocExtraction:
    """`extraction` None (the extractor said "not mine", or raised) counts as nothing."""
    resolver = FixtureVendorResolver(fixture)

    def vendor_field(key_vendor: str, pred_vendor: str) -> FieldResult:
        resolved = resolver.find_vendor(pred_vendor) if pred_vendor else None
        return FieldResult(
            field="vendor",
            expected=key_vendor,
            predicted=pred_vendor or None,
            ok=resolved == key_vendor,
        )

    def term_fields(k: _Item, p: _Item) -> list[FieldResult]:
        key: ExtractedTerm = k.obj
        pred: ExtractedTerm = p.obj
        fs = [vendor_field(key.vendor, pred.vendor)]
        fs += _model_fields("value", key.value, pred.value)
        fs.append(_field("effective_date", key.effective_date, pred.effective_date))
        fs.append(_field("plan_label", key.plan_label, pred.plan_label))
        if key.old_value is not None:
            fs += _model_fields("old_value", key.old_value, pred.old_value)
        return fs

    def fact_fields(k: _Item, p: _Item) -> list[FieldResult]:
        key: InvoiceFact = k.obj
        pred: InvoiceFact = p.obj
        return [vendor_field(key.vendor, pred.vendor)] + [
            _field(name, getattr(key, name), getattr(pred, name))
            for name in (
                "billing_period",
                "invoice_date",
                "amount",
                "invoice_number",
                "seats_billed",
            )
        ]

    def contact_fields(k: _Item, p: _Item) -> list[FieldResult]:
        return [_field("purpose", k.obj.purpose, p.obj.purpose)]

    key_terms, key_facts, key_contacts = expected_items(fixture, path)
    ex = extraction or Extraction(vendor_name=None, engine="none")

    def terms(xs: Sequence[ExtractedTerm]) -> list[_Item]:
        return [_Item(x.id, x.type.value, x.quotes, x) for x in xs]

    def facts(xs: Sequence[InvoiceFact]) -> list[_Item]:
        return [_Item(x.id, "invoice_fact", x.quotes, x) for x in xs]

    def contacts(xs: Sequence[Contact]) -> list[_Item]:
        return [_Item(x.email, "contact", [x.quote], x) for x in xs]

    return DocExtraction(
        path=path,
        duplicate_of=duplicate_of(fixture.document(path)),
        terms=_pair(text, terms(ex.terms), terms(key_terms), lambda i: i.type, term_fields),
        invoice_facts=_pair(
            text, facts(ex.invoice_facts), facts(key_facts), lambda i: None, fact_fields
        ),
        contacts=_pair(
            text,
            contacts(ex.contacts),
            contacts(key_contacts),
            lambda i: i.id.strip().casefold(),
            contact_fields,
            require_agreement=False,  # paired on email, which is a contact's identity
        ),
        error=error,
    )


def _summarize_kind(docs: Sequence[DocExtraction], kind: str, by_type: bool) -> KindSummary:
    s = KindSummary(overall=PrecisionRecall(), quote_found=Rate(), quote_matches_key=Rate())

    def pr(type: str) -> list[PrecisionRecall]:
        if not by_type:
            return [s.overall]
        return [s.overall, s.by_type.setdefault(type, PrecisionRecall())]

    for d in docs:
        score = d.kind(kind)
        for m in score.matched:
            for x in pr(m.type):
                x.matched += 1
                x.expected += 1
                x.predicted += 1
            for f in m.fields:
                s.fields.setdefault(f.field, Rate()).add(f.ok)
            s.quote_found.add(m.quote_found)
            s.quote_matches_key.add(m.quote_matches_key)
        for miss in score.missed:
            for x in pr(miss.type):
                x.expected += 1
        for extra in score.extra:
            for x in pr(extra.type):
                x.predicted += 1
            s.quote_found.add(extra.quote_found)
    s.by_type = dict(sorted(s.by_type.items()))
    return s


def summarize_extraction(fixture: Fixture, docs: Sequence[DocExtraction]) -> ExtractionSummary:
    """Coverage counts a key item only when it was matched in its own document, not in a
    duplicate of it, with every field right, and the validator didn't set the match to
    needs_review (that would hide the finding, ROADMAP 2.3). It checks only the items whose
    documents were scored, and leaves out a finding with none (a `--doc` run)."""
    matched = {
        m.key_id
        for d in docs
        if d.duplicate_of is None
        for m in (*d.terms.matched, *d.invoice_facts.matched)
        if all(f.ok for f in m.fields)
        and not (d.validation and m.pred_id in d.validation.needs_review)
    }
    scored = {d.path for d in docs}
    key_document = {x.id: x.document for x in (*fixture.terms, *fixture.invoice_facts)}
    coverage = {}
    for f in fixture.findings:
        ids = [x for x in (*f.terms, *f.invoice_facts) if key_document[x] in scored]
        if ids:
            coverage[f.id] = [x for x in ids if x not in matched]
    return ExtractionSummary(
        terms=_summarize_kind(docs, TERMS, by_type=True),
        invoice_facts=_summarize_kind(docs, INVOICE_FACTS, by_type=False),
        contacts=_summarize_kind(docs, CONTACTS, by_type=False),
        finding_coverage=coverage,
    )
