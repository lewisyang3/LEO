"""Run stage engines over the synthetic dataset and report the scores (ROADMAP B1).

Engines are chosen by name from `CLASSIFIERS` and `EXTRACTORS`. B3 registers `llm`, and B2
registers `jev`. Each factory takes the `Fixture`.
"""

import datetime
from collections.abc import Callable, Sequence

from leo.config import get_settings
from leo.eval.scoring import (
    KINDS,
    ClassificationSummary,
    DocClassification,
    DocExtraction,
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
from leo.interfaces import Classifier, DocumentText, Extraction, Extractor
from leo.llm import ResponseCache
from leo.pipeline.extract_llm import LLMExtractor
from leo.stubs import StubClassifier, StubExtractor
from leo.types import DocumentClass, LeoModel


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
DEFAULT_CLASSIFIER = "fixture"
DEFAULT_EXTRACTOR = "fixture"


class EvalResult(LeoModel):
    classifier: str
    extractor: str
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
) -> EvalResult:
    """Classify every document, and extract from every document whose key class isn't noise.
    An engine that raises on a document is recorded as an error for it, and the run goes on."""
    clf = CLASSIFIERS[classifier](fixture)
    ext = EXTRACTORS[extractor](fixture)
    paths = list(docs) if docs is not None else [d.path for d in fixture.documents]

    classification = []
    for path in paths:
        try:
            result, error = clf.classify(fixture.document_text(path)), None
        except Exception as e:
            result, error = None, _error(e)
        classification.append(score_classification(fixture, path, result, error))

    extraction = []
    for path in paths:
        if fixture.document(path).doc_class == DocumentClass.noise:
            continue
        doc = fixture.document_text(path, classified=True)
        try:
            ex, error = ext.extract(doc), None
        except Exception as e:
            ex, error = None, _error(e)
        extraction.append(score_extraction(fixture, path, doc.text, ex, error))

    return EvalResult(
        classifier=classifier,
        extractor=extractor,
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
    out.append("finding coverage (every cited term and invoice fact extracted, all fields right)")
    for fid, missing in es.finding_coverage.items():
        status = "covered" if not missing else "not covered: " + ", ".join(missing)
        out.append(f"  {fid}: {status}")
    return "\n".join(out) + "\n"
