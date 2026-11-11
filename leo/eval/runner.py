"""Run stage engines over the synthetic dataset and report the scores.

Engines are chosen by name from `CLASSIFIERS`, `EXTRACTORS`, `VALIDATORS` and `DRAFTERS`: the
`jev` classifier; the `llm`, `script` and `script+llm` extractors (`script+llm` tries
the scripts first and sends the rest to the LLM); the `rules` and `jev` validators; and the
`template` and `llm` drafters. Each factory takes the `Fixture`.

Drafters run on the key's findings and `fixture.ledger()`. The reconciler (`RECONCILERS`)
runs on `fixture.ledger()` at the key's today, and its findings are scored against F1-F5.
Its run on the pipeline's own ledger is `make e2e`.
"""

import datetime
from collections import Counter
from collections.abc import Callable, Sequence

from leo.actions.draft import LLMDrafter, TemplateDrafter
from leo.config import get_settings
from leo.eval.scoring import (
    KINDS,
    ClassificationSummary,
    DocClassification,
    DocExtraction,
    DocValidation,
    DraftScore,
    ExtractionSummary,
    FindingsScore,
    KindSummary,
    PrecisionRecall,
    Rate,
    duplicate_of,
    score_classification,
    score_draft,
    score_extraction,
    score_findings,
    summarize_classification,
    summarize_extraction,
)
from leo.fixtures import Fixture, FixtureClassifier, FixtureDrafter, FixtureExtractor
from leo.interfaces import (
    Classifier,
    DocumentText,
    Drafter,
    Extraction,
    Extractor,
    Reconciler,
    Validator,
)
from leo.jev import jev_client
from leo.llm import ResponseCache
from leo.pipeline.classify import JevClassifier
from leo.pipeline.extract_llm import LLMExtractor
from leo.pipeline.extract_script import ChainExtractor, script_extractor
from leo.pipeline.validate import JevPriorComparer, RuleValidator
from leo.reconcile.reconciler import RuleReconciler
from leo.stubs import StubClassifier, StubDrafter, StubExtractor, StubValidator
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
    "jev": lambda fixture: JevClassifier(jev_client(get_settings())),
}


def _llm_extractor(fixture: Fixture) -> LLMExtractor:
    return LLMExtractor(
        clock=fixture.clock(), cache=ResponseCache(get_settings().leo_llm_cache_dir)
    )


EXTRACTORS: dict[str, Callable[[Fixture], Extractor]] = {
    "fixture": EvalFixtureExtractor,
    "stub": lambda fixture: StubExtractor(),
    "llm": _llm_extractor,
    "script": lambda fixture: script_extractor(),
    "script+llm": lambda fixture: ChainExtractor([script_extractor(), _llm_extractor(fixture)]),
}
# "none" skips validation.
VALIDATORS: dict[str, Callable[[Fixture], Validator] | None] = {
    "none": None,
    "stub": lambda fixture: StubValidator(),
    "rules": lambda fixture: RuleValidator(fixture.clock()),
    "jev": lambda fixture: RuleValidator(
        fixture.clock(), extra=[JevPriorComparer(jev_client(get_settings()))]
    ),
}
# "none" skips drafting.
DRAFTERS: dict[str, Callable[[Fixture], Drafter] | None] = {
    "none": None,
    "fixture": FixtureDrafter,
    "stub": lambda fixture: StubDrafter(),
    "template": lambda fixture: TemplateDrafter(sender=fixture.customer.name),
    "llm": lambda fixture: LLMDrafter(
        ResponseCache(get_settings().leo_llm_cache_dir), sender=fixture.customer.name
    ),
}
# "none" skips the findings check.
RECONCILERS: dict[str, Callable[[Fixture], Reconciler] | None] = {
    "none": None,
    "rule": lambda fixture: RuleReconciler(),
}
DEFAULT_CLASSIFIER = "fixture"
DEFAULT_EXTRACTOR = "fixture"
DEFAULT_VALIDATOR = "none"
DEFAULT_DRAFTER = "none"
DEFAULT_RECONCILER = "rule"


class EvalResult(LeoModel):
    classifier: str
    extractor: str
    validator: str = DEFAULT_VALIDATOR
    drafter: str = DEFAULT_DRAFTER
    dataset: str
    run_at: datetime.datetime
    classification: list[DocClassification]
    classification_summary: ClassificationSummary
    extraction: list[DocExtraction]
    extraction_summary: ExtractionSummary
    drafts: list[DraftScore] = []
    findings: FindingsScore | None = None


def _error(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def run(
    fixture: Fixture,
    classifier: str = DEFAULT_CLASSIFIER,
    extractor: str = DEFAULT_EXTRACTOR,
    docs: Sequence[str] | None = None,
    validator: str = DEFAULT_VALIDATOR,
    drafter: str = DEFAULT_DRAFTER,
    reconciler: str = DEFAULT_RECONCILER,
) -> EvalResult:
    """Classify every document, and extract from every document whose key class isn't noise.
    An engine that raises on a document is recorded as an error for it, and the run goes on.

    With a validator, every extraction is made first, then each is validated before scoring.
    Its `prior_terms` are the terms extracted from every other document of the same key vendor,
    so the result doesn't depend on processing order (the worker passes only earlier ones).
    As in the worker, a prior carries its own validated status and a `needs_review` one isn't
    compared: each extraction is first validated with no priors to get that status."""
    clf = CLASSIFIERS[classifier](fixture)
    ext = EXTRACTORS[extractor](fixture)
    make_validator = VALIDATORS[validator]
    val = make_validator(fixture) if make_validator else None
    paths = list(docs) if docs is not None else [d.path for d in fixture.documents]

    classification = []
    records = getattr(clf, "records", {})  # Jev's probabilities, for the threshold sweep
    for path in paths:
        try:
            result, error = clf.classify(fixture.document_text(path)), None
        except Exception as e:
            result, error = None, _error(e)
        scored = score_classification(fixture, path, result, error)
        scored.probabilities = records.get(path) if result is not None else None
        classification.append(scored)

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

    own: dict[str, list[ExtractedTerm]] = {}  # path -> its terms, validated with no priors
    if val is not None:
        for doc, ex, _ in extracted:
            if ex is None:
                continue
            try:
                checked = val.validate(doc, ex, []).extraction.terms
            except Exception:
                checked = []  # the main pass below records the error
            own[doc.document.path] = [t for t in checked if t.status != TermStatus.needs_review]

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
                for t in own.get(other.document.path, [])
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
        scored.engine = ex.engine if ex is not None else None
        extraction.append(scored)

    make_drafter = DRAFTERS[drafter]
    drafts = run_drafts(fixture, make_drafter(fixture)) if make_drafter else []
    make_reconciler = RECONCILERS[reconciler]
    findings = None
    if make_reconciler:
        found = make_reconciler(fixture).reconcile(fixture.ledger(), fixture.demo_today)
        findings = score_findings(fixture, reconciler, found.findings)

    return EvalResult(
        classifier=classifier,
        extractor=extractor,
        validator=validator,
        drafter=drafter,
        dataset=fixture.dataset,
        run_at=datetime.datetime.now(datetime.UTC).replace(microsecond=0),
        classification=classification,
        classification_summary=summarize_classification(classification),
        extraction=extraction,
        extraction_summary=summarize_extraction(fixture, extraction),
        drafts=drafts,
        findings=findings,
    )


def run_drafts(fixture: Fixture, drafter: Drafter) -> list[DraftScore]:
    """Draft every key finding against the key's ledger. A drafter that raises on a finding
    (for example `LLMOfflineMiss`) is recorded as an error for it."""
    ledger = fixture.ledger()
    records = getattr(drafter, "records", {})
    out = []
    for x in fixture.expected_findings:
        fid = x.finding.id
        try:
            draft, error = drafter.draft(x.finding, ledger), None
        except Exception as e:
            draft, error = None, _error(e)
        fallback = records.get(fid, {}).get("fallback")
        out.append(score_draft(fid, draft, x.expected_action, fallback, error))
    return out


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
    lines.append(f"  {'field accuracy':<32} {_rate(s.field_accuracy)}")
    for field, r in s.fields.items():
        lines.append(f"  {field:<32} {_rate(r)}")
    return lines


def format_report(result: EvalResult) -> str:
    out = [
        f"LEO eval  classifier={result.classifier}  extractor={result.extractor}"
        f"  validator={result.validator}  drafter={result.drafter}"
        f"  dataset={result.dataset}  {result.run_at.isoformat()}",
        "",
    ]
    out += format_classification(
        result.classification, result.classification_summary, "cited by a planted finding"
    )

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
            f"  quotes found {_pct(quotes.rate())}  [{d.engine or 'no answer'}]{dup}"
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
    out += _engine_summary(result.extraction)
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
    if result.findings:
        out += _findings_report(result.findings)
    if result.drafts:
        out += _draft_report(result.drafts)
    return "\n".join(out) + "\n"


def _engine_summary(docs: list[DocExtraction]) -> list[str]:
    """Which engine answered each document, and how many reached the LLM."""
    engines = Counter(d.engine or "no answer" for d in docs)
    llm = sum(n for e, n in engines.items() if e.startswith("llm:"))
    out = ["extraction engines"]
    out += [f"  {e:<32} {n}" for e, n in sorted(engines.items())]
    out.append(f"  {'reached the LLM':<32} {llm}/{len(docs)} documents")
    return out


def format_classification(
    docs: list[DocClassification], cs: ClassificationSummary, star_legend: str
) -> list[str]:
    """The classification section of the report. `star_legend` says what a starred row
    (`matters`) means, because the holdout set (`leo.eval.holdout`) defines it differently."""
    out = [f"== Classification ({len(docs)} documents) =="]
    width = max((len(d.path) for d in docs), default=0)
    has_p = any(d.p_noise is not None for d in docs)
    p_head = "  p(noise)" if has_p else ""
    out.append(f"   {'path':<{width}}  {'key':<12}   {'predicted':<12}  conf{p_head}")
    for d in docs:
        mark = "✓" if d.ok else "✗"
        predicted = d.predicted.value if d.predicted else "ERROR"
        conf = f"{d.confidence:.2f}" if d.confidence is not None else "  - "
        p_noise = ""
        if has_p:
            p_noise = f"  {d.p_noise:8.2f}" if d.p_noise is not None else f"  {'-':>8}"
        star = "  *" if d.matters else ""
        out.append(
            f" {mark} {d.path:<{width}}  {d.expected.value:<12} → {predicted:<12}  {conf}"
            f"{p_noise}{star}"
        )
        if d.error:
            out.append(f"     error: {d.error}")
    out += [
        f"   (* {star_legend})",
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
    if cs.threshold_sweep:
        out += _sweep_report(docs, cs)
    return out


def _sweep_report(docs: list[DocClassification], cs: ClassificationSummary) -> list[str]:
    noise = [d for d in docs if d.predicted == DocumentClass.noise]
    out = ["", f"noise-labeled documents, for a spot check ({len(noise)})"]
    for d in sorted(noise, key=lambda d: d.p_noise or 0.0):
        p = f"{d.p_noise:.2f}" if d.p_noise is not None else "-"
        out.append(f"  {d.path:<48} p(noise) {p}  key {d.expected.value}")
    out += ["noise threshold sweep (noise when p(noise) >= threshold)"]
    out.append(f"  {'threshold':<10} {'matters recall':<18} noise false-pass")
    for p in cs.threshold_sweep:
        out.append(
            f"  {p.threshold:<10.3f} {_rate(p.matters_recall):<18} {_rate(p.noise_false_pass)}"
        )
    rec = cs.recommended_threshold
    out.append(
        "lowest threshold with full matters recall: "
        + (f"{rec:.3f}" if rec is not None else "none in the sweep")
    )
    return out


def _findings_report(score: FindingsScore) -> list[str]:
    out = ["", f"== Findings (reconciler={score.reconciler}, on the key's ledger) =="]
    for f in score.expected:
        wrong = [
            name
            for name, ok in (
                ("deadline", f.deadline_ok),
                ("send-by", f.send_by_ok),
                ("impact", f.impact_ok),
            )
            if not ok
        ]
        note = "not found" if not f.found else (f"wrong: {', '.join(wrong)}" if wrong else "")
        out.append(f" {'✓' if f.ok else '✗'} {f.finding}  {f.kind}  {note}".rstrip())
    out += [f" ✗ extra  {x}" for x in score.extra]
    found = sum(f.ok for f in score.expected)
    out.append(f"findings {found}/{len(score.expected)} right, {len(score.extra)} extra")
    return out


def _draft_report(drafts: list[DraftScore]) -> list[str]:
    out = ["", f"== Drafts ({len(drafts)} findings) =="]
    for d in drafts:
        mark = "✓" if d.ok else "✗"
        if d.draft is None:
            out.append(f" {mark} {d.finding}  error: {d.error}")
            continue
        a = d.draft
        flags = [
            name
            for name, ok in (
                ("type", d.type_ok),
                ("alternative", d.alternative_ok),
                ("to", d.to_ok),
                ("pre_step", d.pre_step_ok),
            )
            if not ok
        ]
        note = "  (template fallback)" if d.fallback else ""
        out.append(f" {mark} {d.finding}  {a.type.value} → {a.to or '(no recipient)'}{note}")
        if flags:
            out.append(f"     wrong: {', '.join(flags)}")
        if d.missing:
            out.append(f"     missing: {', '.join(repr(m) for m in d.missing)}")
        if a.alternative:
            out.append(f"     alternative: {a.alternative.value}")
        if a.pre_step:
            out.append(f"     pre-step: {a.pre_step}")
        out.append(f"     subject: {a.subject}")
        out += ["     | " + line for line in a.body.splitlines()]
    ok = sum(d.ok for d in drafts)
    fallbacks = sum(bool(d.fallback) for d in drafts)
    out.append(f"drafts ok {ok}/{len(drafts)}, template fallbacks {fallbacks}")
    return out
