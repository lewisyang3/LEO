"""Run stage engines over the synthetic dataset and report the scores (ROADMAP B1).

Engines are chosen by name from `CLASSIFIERS`, `EXTRACTORS` and `VALIDATORS`. B3 registers
`llm`, B4 registers `rules` and `jev`, and B2 registers its classifier. Each factory takes the
`Fixture`.
"""

import datetime
from collections import Counter
from collections.abc import Callable, Sequence

from leo.config import get_settings
from leo.eval.scoring import (
    KINDS,
    ClassificationSummary,
    DocClassification,
    DocExtraction,
    DocValidation,
    ExtractionSummary,
    KindSummary,
    PrecisionRecall,
    Rate,
    duplicate_of,
    score_classification,
    score_extraction,
    summarize_classification,
    summarize_extraction,
)
from leo.fixtures import Fixture, FixtureClassifier, FixtureExtractor
from leo.interfaces import Classifier, DocumentText, Extraction, Extractor, Validator
from leo.jev import jev_client
from leo.llm import ResponseCache
from leo.pipeline.extract_llm import LLMExtractor
from leo.pipeline.validate import JevPriorComparer, RuleValidator
from leo.stubs import StubClassifier, StubExtractor, StubValidator
from leo.types import DocumentClass, ExtractedTerm, LeoModel, TermStatus


class EvalFixtureExtractor:
    """`FixtureExtractor`, except that a duplicate document (02, 07) gets its original's items,
    with `document` set to the duplicate's path. That is what is actually in the duplicate, so
    the reference engine scores 100% under the duplicate rule in `leo.eval.scoring`.
    `FixtureExtractor` itself keeps its D2 behavior (no items for the forward), which the worker
    relies on."""

    def __init__(self, fixture: Fixture) -> None:
        self.fixture = fixture
        self.inner = FixtureExtractor(fixture)

    def extract(self, doc: DocumentText) -> Extraction | None:
        path = doc.document.path
        original = duplicate_of(self.fixture.document(path))
        if original is None:
            return self.inner.extract(doc)
        ex = self.inner.extract(self.fixture.document_text(original, classified=True))
        assert ex is not None
        here = {"document": path}
        return ex.model_copy(
            update={
                "terms": [x.model_copy(update=here) for x in ex.terms],
                "invoice_facts": [x.model_copy(update=here) for x in ex.invoice_facts],
                "contacts": [x.model_copy(update=here) for x in ex.contacts],
            }
        )


CLASSIFIERS: dict[str, Callable[[Fixture], Classifier]] = {
    "fixture": FixtureClassifier,
    "stub": lambda fixture: StubClassifier(),
}
EXTRACTORS: dict[str, Callable[[Fixture], Extractor]] = {
    "fixture": EvalFixtureExtractor,
    "stub": lambda fixture: StubExtractor(),
    "llm": lambda fixture: LLMExtractor(
        clock=fixture.clock(), cache=ResponseCache(get_settings().leo_llm_cache_dir)
    ),
}
# "none" skips validation (B1's behavior).
VALIDATORS: dict[str, Callable[[Fixture], Validator] | None] = {
    "none": None,
    "stub": lambda fixture: StubValidator(),
    "rules": lambda fixture: RuleValidator(fixture.clock()),
    "jev": lambda fixture: RuleValidator(
        fixture.clock(), extra=[JevPriorComparer(jev_client(get_settings()))]
    ),
}
DEFAULT_CLASSIFIER = "fixture"
DEFAULT_EXTRACTOR = "fixture"
DEFAULT_VALIDATOR = "none"


class EvalResult(LeoModel):
    classifier: str
    extractor: str
    validator: str = DEFAULT_VALIDATOR
    dataset: str
    run_at: datetime.datetime
    classification: list[DocClassification]
    classification_summary: ClassificationSummary
    extraction: list[DocExtraction]
    extraction_summary: ExtractionSummary


def _error(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def run(
    fixture: Fixture,
    classifier: str = DEFAULT_CLASSIFIER,
    extractor: str = DEFAULT_EXTRACTOR,
    docs: Sequence[str] | None = None,
    validator: str = DEFAULT_VALIDATOR,
) -> EvalResult:
    """Classify every document, and extract from every document whose key class isn't noise.
    An engine that raises on a document is recorded as an error for it, and the run goes on.

    With a validator, every extraction is made first, then each is validated before scoring.
    Its `prior_terms` are the terms extracted from every other document of the same key vendor,
    so the result doesn't depend on processing order (the worker passes only earlier ones)."""
    clf = CLASSIFIERS[classifier](fixture)
    ext = EXTRACTORS[extractor](fixture)
    make_validator = VALIDATORS[validator]
    val = make_validator(fixture) if make_validator else None
    paths = list(docs) if docs is not None else [d.path for d in fixture.documents]

    classification = []
    for path in paths:
        try:
            result, error = clf.classify(fixture.document_text(path)), None
        except Exception as e:
            result, error = None, _error(e)
        classification.append(score_classification(fixture, path, result, error))

    extracted: list[tuple[DocumentText, Extraction | None, str | None]] = []
    for path in paths:
        if fixture.document(path).doc_class == DocumentClass.noise:
            continue
        doc = fixture.document_text(path, classified=True)
        try:
            ex, error = ext.extract(doc), None
        except Exception as e:
            ex, error = None, _error(e)
        extracted.append((doc, ex, error))

    extraction = []
    for doc, ex, error in extracted:
        path = doc.document.path
        validation = None
        if val is not None and ex is not None:
            vendor = fixture.document(path).vendor
            prior: list[ExtractedTerm] = [
                t
                for other, other_ex, _ in extracted
                if other_ex is not None
                and other.document.path != path
                and vendor is not None
                and fixture.document(other.document.path).vendor == vendor
                for t in other_ex.terms
            ]
            try:
                v = val.validate(doc, ex, prior)
            except Exception as e:
                validation = DocValidation(error=_error(e))
            else:
                ex = v.extraction
                validation = DocValidation(
                    engine=v.engine,
                    vendor_ok=v.vendor_ok,
                    issues=v.issues,
                    needs_review=[
                        x.id
                        for x in (*ex.terms, *ex.invoice_facts)
                        if x.status == TermStatus.needs_review
                    ],
                )
        scored = score_extraction(fixture, path, doc.text, ex, error)
        scored.validation = validation
        extraction.append(scored)

    return EvalResult(
        classifier=classifier,
        extractor=extractor,
        validator=validator,
        dataset=fixture.dataset,
        run_at=datetime.datetime.now(datetime.UTC).replace(microsecond=0),
        classification=classification,
        classification_summary=summarize_classification(classification),
        extraction=extraction,
        extraction_summary=summarize_extraction(fixture, extraction),
    )


# --- Report ----------------------------------------------------------------------------------


def _pct(r: float | None) -> str:
    return "  n/a" if r is None else f"{r:5.1%}"


def _rate(r: Rate) -> str:
    return f"{r.hit}/{r.total} ({_pct(r.rate()).strip()})"


def _pr(x: PrecisionRecall) -> str:
    return (
        f"P {_pct(x.precision())}  R {_pct(x.recall())}"
        f"  ({x.matched} matched, {x.expected} expected, {x.predicted} predicted)"
    )


def _counts(score) -> str:
    matched = len(score.matched)
    return f"{matched}/{matched + len(score.missed)}/{matched + len(score.extra)}"


def _doc_rates(d: DocExtraction) -> tuple[Rate, Rate]:
    fields, quotes = Rate(), Rate()
    for kind in KINDS:
        s = d.kind(kind)
        for m in s.matched:
            for f in m.fields:
                fields.add(f.ok)
            quotes.add(m.quote_found)
        for e in s.extra:
            quotes.add(e.quote_found)
    return fields, quotes


def _kind_summary(name: str, s: KindSummary) -> list[str]:
    lines = [f"{name}: {_pr(s.overall)}"]
    for t, x in s.by_type.items():
        lines.append(f"  {t:<16} {_pr(x)}")
    lines.append(f"  {'quote found':<32} {_rate(s.quote_found)}")
    lines.append(f"  {'quote matches key':<32} {_rate(s.quote_matches_key)}")
    for field, r in s.fields.items():
        lines.append(f"  {field:<32} {_rate(r)}")
    return lines


def format_report(result: EvalResult) -> str:
    out = [
        f"LEO eval  classifier={result.classifier}  extractor={result.extractor}"
        f"  validator={result.validator}"
        f"  dataset={result.dataset}  {result.run_at.isoformat()}",
        "",
        f"== Classification ({len(result.classification)} documents) ==",
    ]
    width = max((len(d.path) for d in result.classification), default=0)
    out.append(f"   {'path':<{width}}  {'key':<12}   {'predicted':<12}  conf")
    for d in result.classification:
        mark = "✓" if d.ok else "✗"
        predicted = d.predicted.value if d.predicted else "ERROR"
        conf = f"{d.confidence:.2f}" if d.confidence is not None else "  - "
        star = "  *" if d.matters else ""
        out.append(
            f" {mark} {d.path:<{width}}  {d.expected.value:<12} → {predicted:<12}  {conf}{star}"
        )
        if d.error:
            out.append(f"     error: {d.error}")
    cs = result.classification_summary
    out += [
        "   (* cited by a planted finding)",
        "",
        f"accuracy                         {_rate(cs.accuracy)}",
        f"recall on documents that matter  {_rate(cs.matters_recall)}",
        f"noise false-pass                 {_rate(cs.noise_false_pass)}",
        "mean confidence                  "
        + (f"{cs.mean_confidence:.2f}" if cs.mean_confidence is not None else "n/a"),
        "confusion (key → predicted: count)",
    ]
    for key, row in cs.confusion.items():
        out.append(f"  {key:<13} " + "  ".join(f"{k}: {v}" for k, v in sorted(row.items())))

    out += [
        "",
        f"== Extraction ({len(result.extraction)} documents) ==",
        "   counts are matched/expected/predicted",
    ]
    width = max((len(d.path) for d in result.extraction), default=0)
    for d in result.extraction:
        fields, quotes = _doc_rates(d)
        dup = f"  [duplicate of {d.duplicate_of}]" if d.duplicate_of else ""
        out.append(
            f" {d.path:<{width}}  terms {_counts(d.terms)}  facts {_counts(d.invoice_facts)}"
            f"  contacts {_counts(d.contacts)}  fields {_pct(fields.rate())}"
            f"  quotes found {_pct(quotes.rate())}{dup}"
        )
        if d.error:
            out.append(f"     error: {d.error}")
        if v := d.validation:
            if v.error:
                out.append(f"     validator error: {v.error}")
            elif not v.vendor_ok:
                out.append("     vendor not ok")
            for i in v.issues:
                out.append(f"     issue  {i.check} {i.item or '-'}: {i.message}")
            if v.needs_review:
                out.append(f"     needs_review: {', '.join(v.needs_review)}")
        for kind in KINDS:
            s = d.kind(kind)
            for miss in s.missed:
                out.append(f"     missed {miss.key_id} ({miss.type})")
            for extra in s.extra:
                found = "" if extra.quote_found else "  (quote not found)"
                out.append(f"     extra  {extra.type}: {extra.first_quote!r}{found}")
            for m in s.matched:
                for f in m.fields:
                    if not f.ok:
                        out.append(
                            f"     wrong  {m.key_id}.{f.field}: {f.expected} ≠ {f.predicted}"
                        )
                if not m.quote_found:
                    out.append(f"     quote  {m.key_id}: a predicted quote is not in the document")
                elif not m.quote_matches_key:
                    out.append(f"     quote  {m.key_id}: first quote doesn't overlap the key's")

    es = result.extraction_summary
    out += ["", "== Summary =="]
    out += _kind_summary("terms", es.terms)
    out += _kind_summary("invoice facts", es.invoice_facts)
    out += _kind_summary("contacts", es.contacts)
    validated = [d.validation for d in result.extraction if d.validation is not None]
    if validated:
        engines = sorted({v.engine for v in validated if v.engine})
        checks = Counter(i.check for v in validated for i in v.issues)
        out.append(f"validation ({', '.join(engines) or 'error'})")
        out.append(f"  {'vendor ok':<32} {sum(v.vendor_ok for v in validated)}/{len(validated)}")
        out.append(
            f"  {'items set to needs_review':<32} {sum(len(v.needs_review) for v in validated)}"
        )
        out.append(f"  {'validator errors':<32} {sum(v.error is not None for v in validated)}")
        for check, n in sorted(checks.items()):
            out.append(f"  issue {check:<26} {n}")
    out.append(
        "finding coverage (every cited term and invoice fact extracted, all fields right,"
        " none needs_review)"
    )
    for fid, missing in es.finding_coverage.items():
        status = "covered" if not missing else "not covered: " + ", ".join(missing)
        out.append(f"  {fid}: {status}")
    return "\n".join(out) + "\n"
