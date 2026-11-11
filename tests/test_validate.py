"""The validator. No network: a fake client stands in for Jev, except in the
opt-in live test."""

import datetime
import json
import os
from decimal import Decimal
from types import SimpleNamespace

import pytest
from typesafe_sdk import TypeSafeError

from leo.clock import FixedClock
from leo.config import Settings
from leo.eval.runner import run
from leo.fixtures import FixtureExtractor, load_fixture
from leo.interfaces import DocumentText, Extraction, Validator
from leo.jev import JevUnavailable, jev_client
from leo.llm import ResponseCache
from leo.pipeline.extract_llm import LLMExtractor
from leo.pipeline.validate import (
    AMOUNT_IMPLAUSIBLE,
    CONFLICTS_PRIOR,
    DATE_IMPLAUSIBLE,
    DATE_UNPARSED,
    QUOTE_NOT_FOUND,
    VENDOR_MISSING,
    VENDOR_NOT_IN_SOURCE,
    JevPriorComparer,
    RulePriorComparer,
    RuleValidator,
    jev_state,
)
from leo.types import (
    DocumentClass,
    InvoiceFact,
    TermStatus,
    extracted_term_adapter,
)
from leo.types import SourceDocument as Doc

FX = load_fixture()
TODAY = datetime.date(2026, 11, 2)
CLOCK = FixedClock(TODAY)
TEXT = "From: Lumenpost Billing\nGrowth plan: $339/month from Dec 1.\nInvoice INV-1: $339.\n"
PATH = "inbox/x.eml"


def _doc(text: str = TEXT, path: str = PATH) -> DocumentText:
    return DocumentText(document=Doc(path=path, doc_class="price_notice", vendor=None), text=text)


def _price(id="t1", amount=339, eff="2026-12-01", document=PATH, quote="$339/month", **kw):
    return extracted_term_adapter.validate_python(
        {
            "id": id,
            "vendor": "Lumenpost Billing",
            "document": document,
            "type": "price",
            "value": {"amount": Decimal(amount), "currency": "USD", "per": "month"},
            "quotes": [quote],
            "plan_label": "Growth",
            "effective_date": eff,
            **kw,
        }
    )


def _window(id="w1", days=30, applies_to="vendor_price_change", document=PATH, deliver_to=None):
    return extracted_term_adapter.validate_python(
        {
            "id": id,
            "vendor": "Lumenpost Billing",
            "document": document,
            "type": "notice_window",
            "value": {
                "days": days,
                "relative_to": "price_change",
                "applies_to": applies_to,
                "deliver_to": deliver_to,
                "next_deadline": None,
            },
            "quotes": ["Growth plan"],
        }
    )


def _fact(id="f1", amount=339, invoice_date="2026-10-01", billing_period="2026-10", **kw):
    return InvoiceFact(
        id=id,
        vendor="Lumenpost Billing",
        document=PATH,
        billing_period=billing_period,
        invoice_date=invoice_date,
        amount=Decimal(amount),
        quotes=["Invoice INV-1"],
        **kw,
    )


def _ex(terms=(), facts=(), contacts=(), vendor="Lumenpost Billing") -> Extraction:
    return Extraction(
        vendor_name=vendor,
        terms=list(terms),
        invoice_facts=list(facts),
        contacts=list(contacts),
        engine="test",
    )


def _validate(ex: Extraction, prior=(), text: str = TEXT, extra=()):
    return RuleValidator(CLOCK, extra=extra).validate(_doc(text), ex, list(prior))


def _checks(v) -> list[str]:
    return [i.check for i in v.issues]


def test_is_a_validator():
    assert isinstance(RuleValidator(CLOCK), Validator)


def test_clean_extraction_passes():
    v = _validate(_ex([_price()], [_fact()]))
    assert v.issues == [] and v.vendor_ok and v.engine == "rules"
    assert all(
        x.status == TermStatus.current for x in (*v.extraction.terms, *v.extraction.invoice_facts)
    )


# --- Vendor ----------------------------------------------------------------------------------


def test_vendor_missing_fails_closed_and_keeps_every_item():
    v = _validate(_ex([_price()], [_fact()], vendor=None))
    assert _checks(v) == [VENDOR_MISSING] and not v.vendor_ok
    assert len(v.extraction.terms) == 1 and len(v.extraction.invoice_facts) == 1
    assert {x.status for x in (*v.extraction.terms, *v.extraction.invoice_facts)} == {
        TermStatus.needs_review
    }


def test_vendor_not_in_source_fails_closed():
    v = _validate(_ex([_price()], vendor="Lumenpost Inc"))
    assert _checks(v) == [VENDOR_NOT_IN_SOURCE] and not v.vendor_ok
    assert v.extraction.terms[0].status == TermStatus.needs_review


@pytest.mark.parametrize("name", ["", "  ", "\n"])
def test_blank_vendor_name_is_missing_and_fails_closed(name):
    v = _validate(_ex([_price()], [_fact()], vendor=name))
    assert _checks(v) == [VENDOR_MISSING] and not v.vendor_ok
    assert {x.status for x in (*v.extraction.terms, *v.extraction.invoice_facts)} == {
        TermStatus.needs_review
    }


def test_vendor_name_is_matched_stripped():
    assert _validate(_ex(vendor=" Lumenpost Billing\n")).vendor_ok


def test_vendor_match_ignores_case():
    assert _validate(_ex(vendor="LUMENPOST billing")).vendor_ok


# --- Quotes ----------------------------------------------------------------------------------


def test_quote_off_by_one_character_is_not_found():
    v = _validate(_ex([_price(quote="$339/months"), _price(id="t2", eff="2027-01-01")]))
    assert _checks(v) == [QUOTE_NOT_FOUND] and v.issues[0].item == "t1"
    assert [t.status for t in v.extraction.terms] == [TermStatus.needs_review, TermStatus.current]
    assert v.vendor_ok


def test_contact_quote_not_found_is_vendor_level():
    contact = {"email": "billing@lumenpost.io", "purpose": "billing", "document": PATH}
    from leo.types import Contact

    v = _validate(_ex([_price()], contacts=[Contact(**contact, quote="not in the text")]))
    assert _checks(v) == [QUOTE_NOT_FOUND] and v.issues[0].item == ""
    assert "billing@lumenpost.io" in v.issues[0].message
    assert v.extraction.terms[0].status == TermStatus.current
    assert len(v.extraction.contacts) == 1


# --- Dates -----------------------------------------------------------------------------------


@pytest.mark.parametrize("period", ["2026-13", "Oct 2026", "2026-11-04/2026-10-05", "2026-10-05"])
def test_bad_billing_period(period):
    assert _checks(_validate(_ex(facts=[_fact(billing_period=period)]))) == [DATE_UNPARSED]


@pytest.mark.parametrize("period", ["2026-10", "2026-10-05/2026-11-04"])
def test_good_billing_period(period):
    assert _validate(_ex(facts=[_fact(billing_period=period)])).issues == []


def test_invoice_date_after_today():
    assert _checks(_validate(_ex(facts=[_fact(invoice_date="2026-11-03")]))) == [DATE_IMPLAUSIBLE]


def test_date_out_of_range():
    assert _checks(_validate(_ex([_price(eff="1999-12-31")]))) == [DATE_IMPLAUSIBLE]


# --- Amounts ---------------------------------------------------------------------------------


@pytest.mark.parametrize("amount", [-5, 10_000_001])
def test_implausible_price(amount):
    assert _checks(_validate(_ex([_price(amount=amount)]))) == [AMOUNT_IMPLAUSIBLE]


@pytest.mark.parametrize("amount", [-1])
def test_implausible_invoice_amount(amount):
    assert _checks(_validate(_ex(facts=[_fact(amount=amount)]))) == [AMOUNT_IMPLAUSIBLE]


@pytest.mark.parametrize(
    "pct, bad", [(0, False), (-1, True), (150, True), (100, False), (Decimal("3.5"), False)]
)
def test_percentages(pct, bad):
    cap = extracted_term_adapter.validate_python(
        {
            "id": "c1",
            "vendor": "x",
            "document": PATH,
            "type": "price_cap",
            "value": {"max_increase_pct": pct, "applies_to": "renewal_term", "baseline": None},
            "quotes": ["Growth plan"],
        }
    )
    assert _checks(_validate(_ex([cap]))) == ([AMOUNT_IMPLAUSIBLE] if bad else [])


def test_zero_price_and_invoice_amount_pass():
    # A free plan or a credit month is $0; a price lock is a 0% cap (test_percentages).
    assert _checks(_validate(_ex([_price(amount=0)], facts=[_fact(amount=0)]))) == []


def test_zero_days():
    assert _checks(_validate(_ex([_window(days=0)]))) == [AMOUNT_IMPLAUSIBLE]


def test_bad_currency():
    t = _price()
    t.value.currency = "$"
    assert _checks(_validate(_ex([t]))) == [AMOUNT_IMPLAUSIBLE]


def test_none_fields_are_skipped():
    t = _price()
    t.value.amount = None
    t.value.currency = None
    assert _validate(_ex([t])).issues == []


# --- Prior terms: rules ----------------------------------------------------------------------

OTHER = "inbox/other.eml"


def test_same_key_different_amount_conflicts():
    v = _validate(_ex([_price()]), prior=[_price(id="p1", amount=299, document=OTHER)])
    assert _checks(v) == [CONFLICTS_PRIOR]
    assert v.extraction.terms[0].status == TermStatus.needs_review


@pytest.mark.parametrize(
    "prior",
    [
        _price(id="p1", amount=299, eff="2025-03-03", document=OTHER),  # a price change
        _price(id="p1", document=OTHER),  # a restatement
        _price(id="p1", amount=299),  # same document
        _price(id="p1", amount=299, document=OTHER, status="needs_review"),
        _price(id="p1", amount=299, document=OTHER, plan_label="Starter"),
        _price(id="p1", amount=299, document=OTHER, agreement_ref="LP-1"),
    ],
)
def test_not_a_conflict(prior):
    assert _validate(_ex([_price()]), prior=[prior]).issues == []


def test_free_text_differences_are_not_conflicts():
    new = _window(deliver_to="billing@lumenpost.io")
    prior = _window(id="p1", document=OTHER, deliver_to="Billing team")
    assert RulePriorComparer().compare(new, [prior]).issues == []


def test_windows_for_different_purposes_are_not_compared():
    prior = _window(id="p1", days=60, applies_to="non_renewal", document=OTHER)
    assert RulePriorComparer().compare(_window(), [prior]).issues == []
    same = _window(id="p2", days=60, document=OTHER)
    assert len(RulePriorComparer().compare(_window(), [same]).issues) == 1


# --- Prior terms: Jev ------------------------------------------------------------------------


class FakeJev:
    def __init__(self, choice="restates", probabilities=None, error=None):
        self.calls = []
        self.choice = choice
        self.probabilities = probabilities or {choice: 0.9}
        self.error = error

    def system_one(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        answer = SimpleNamespace(choice=self.choice, probabilities=self.probabilities)
        return SimpleNamespace(choices={"relation": answer})


def _jev(new, prior, **kw):
    fake = FakeJev(**kw)
    v = _validate(_ex([new]), prior=prior, extra=[JevPriorComparer(fake)])
    return fake, v


def test_jev_not_called_without_candidates():
    fake, v = _jev(_price(), [])
    assert fake.calls == [] and v.engine == "rules+jev"
    fake, _ = _jev(_price(), [_price(id="p1", amount=299, eff="2025-03-03", document=OTHER)])
    assert fake.calls == []  # a different effective date never reaches Jev


def test_jev_called_once_per_prior_with_json_state():
    priors = [_price(id="p1", document=OTHER), _price(id="p2", document="inbox/third.eml")]
    fake, v = _jev(_price(), priors)
    assert len(fake.calls) == 2 and v.issues == []
    state, questions = fake.calls[0]
    assert set(questions) == {"relation"}
    json.dumps(state)  # JSON-native: no Decimal or date
    assert set(state) == {"new_term", "prior_term"}
    assert not {"id", "vendor", "status", "document"} & set(state["new_term"])
    assert state["new_term"]["value"]["amount"] == 339.0
    assert state["new_term"]["effective_date"] == "2026-12-01"


@pytest.mark.parametrize(
    "choice, probabilities, conflict",
    [
        ("conflicts", {"conflicts": 0.7, "restates": 0.3}, True),
        ("conflicts", {"conflicts": 0.5, "restates": 0.25, "unrelated": 0.25}, False),
        ("restates", {"restates": 0.8, "conflicts": 0.2}, False),
    ],
)
def test_jev_threshold(choice, probabilities, conflict):
    _, v = _jev(
        _price(), [_price(id="p1", document=OTHER)], choice=choice, probabilities=probabilities
    )
    assert _checks(v) == ([CONFLICTS_PRIOR] if conflict else [])
    expected = TermStatus.needs_review if conflict else TermStatus.current
    assert v.extraction.terms[0].status == expected


def test_jev_error_falls_back_to_rules():
    prior = [_price(id="p1", amount=299, document=OTHER)]
    error = TypeSafeError("connection refused")
    fake, v = _jev(_price(), prior, error=error)
    assert len(fake.calls) == 1
    assert _checks(v) == [CONFLICTS_PRIOR]  # from the rule comparer
    assert v.engine == "rules+jev(error)"


def test_jev_state_round_trips():
    json.dumps(jev_state(_window(), _window(id="p1", document=OTHER)))


@pytest.mark.parametrize("key", [None, "", "  "])
def test_missing_jev_key_fails_fast(key):
    with pytest.raises(JevUnavailable, match="JEV_API_KEY"):
        jev_client(Settings(_env_file=None, jev_api_key=key))


# --- The answer key and B3's cache pass clean ------------------------------------------------

NON_NOISE = [d.path for d in FX.documents if d.doc_class != DocumentClass.noise]


def test_answer_key_passes_clean():
    validator = RuleValidator(FX.clock())
    ex = FixtureExtractor(FX)
    for path in NON_NOISE:
        vendor = FX.document(path).vendor
        prior = [t for t in FX.terms if t.vendor == vendor and t.document != path]
        v = validator.validate(
            FX.document_text(path, classified=True),
            ex.extract(FX.document_text(path, classified=True)),
            prior,
        )
        assert v.issues == [] and v.vendor_ok, (path, v.issues)


def test_eval_with_rules_validator_on_the_key():
    result = run(FX, validator="rules")
    assert all(d.validation and not d.validation.issues for d in result.extraction)
    assert result.extraction_summary.finding_coverage == {f"F{i}": [] for i in range(1, 6)}


def test_coverage_excludes_items_set_to_needs_review():
    class Flagger:
        def validate(self, doc, extraction, prior_terms):
            from leo.interfaces import Validated

            return Validated(
                extraction=extraction.model_copy(
                    update={
                        "terms": [
                            t.model_copy(update={"status": TermStatus.needs_review})
                            for t in extraction.terms
                        ]
                    }
                ),
                engine="flagger",
                vendor_ok=extraction.vendor_name is not None,
            )

    import leo.eval.runner as runner

    runner.VALIDATORS["flagger"] = lambda fixture: Flagger()
    try:
        result = run(FX, validator="flagger")
    finally:
        del runner.VALIDATORS["flagger"]
    coverage = result.extraction_summary.finding_coverage
    assert all(coverage[f"F{i}"] for i in range(1, 6))  # every finding cites a term


def test_coverage_excludes_documents_the_validator_raised_on():
    class Crasher:
        def validate(self, doc, extraction, prior_terms):
            raise RuntimeError("boom")

    import leo.eval.runner as runner

    runner.VALIDATORS["crasher"] = lambda fixture: Crasher()
    try:
        result = run(FX, validator="crasher")
    finally:
        del runner.VALIDATORS["crasher"]
    assert all(d.validation and d.validation.error for d in result.extraction)
    coverage = result.extraction_summary.finding_coverage
    assert all(coverage[f"F{i}"] for i in range(1, 6))


def test_cached_llm_extraction_passes_clean():
    settings = Settings(_env_file=None, leo_llm_offline=True)
    if not list((settings.leo_llm_cache_dir / "extract").glob("*.json")):
        pytest.skip("no committed extraction cache")
    import leo.eval.runner as runner

    runner.EXTRACTORS["llm-offline"] = lambda fixture: LLMExtractor(
        clock=fixture.clock(), cache=ResponseCache(settings.leo_llm_cache_dir), settings=settings
    )
    try:
        result = run(FX, extractor="llm-offline", validator="rules")
    finally:
        del runner.EXTRACTORS["llm-offline"]
    for d in result.extraction:
        assert d.validation and d.validation.issues == [] and d.validation.vendor_ok, d.path
    assert result.extraction_summary.finding_coverage == {f"F{i}": [] for i in range(1, 6)}


# --- Live Jev (opt-in) -----------------------------------------------------------------------

LIVE = os.environ.get("LEO_LIVE") == "1"


@pytest.mark.skipif(not LIVE, reason="set LEO_LIVE=1 and JEV_API_KEY to call Jev")
def test_live_jev_restatement_and_conflict():
    comparer = JevPriorComparer(jev_client())
    # Doc 02's Growth term vs doc 01's identical one.
    restated = comparer.compare(_price(), [_price(id="p1", document=OTHER)])
    assert not restated.error and restated.issues == []
    # Same period, different price: a conflict.
    conflict = comparer.compare(_price(), [_price(id="p1", amount=299, document=OTHER)])
    assert not conflict.error and len(conflict.issues) == 1
