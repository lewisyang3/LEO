"""The eval harness (ROADMAP B1)."""

from decimal import Decimal

from leo.eval.__main__ import main
from leo.eval.runner import EvalFixtureExtractor, EvalResult, format_report, run
from leo.eval.scoring import (
    score_extraction,
    summarize_extraction,
)
from leo.fixtures import FixtureExtractor, load_fixture
from leo.interfaces import Extraction

FX = load_fixture()
CORVANA = "contracts/corvana_subscription_agreement.md"
LUMENPOST = "inbox/01_lumenpost_price_change_notice.eml"
FORWARD = "inbox/02_fw_lumenpost_price_change_notice.eml"
RESYNC = "inbox/07_kestrel_invoice_2026_10_resynced.eml"


def _rates(summary):
    """Every precision, recall and field rate in an extraction kind summary."""
    rates = [summary.overall.precision(), summary.overall.recall()]
    rates += [x.precision() for x in summary.by_type.values()]
    rates += [x.recall() for x in summary.by_type.values()]
    rates += [r.rate() for r in summary.fields.values()]
    rates += [summary.quote_found.rate(), summary.quote_matches_key.rate()]
    return rates


def _score(path: str, extraction: Extraction):
    text = FX.document_text(path).text
    return score_extraction(FX, path, text, extraction)


def _corvana() -> Extraction:
    ex = FixtureExtractor(FX).extract(FX.document_text(CORVANA, classified=True))
    assert ex is not None
    return ex


def test_fixture_engines_score_everything():
    result = run(FX)
    cs = result.classification_summary
    assert cs.accuracy.rate() == 1.0
    assert cs.matters_recall.hit == cs.matters_recall.total == 9
    assert cs.noise_false_pass.hit == 0 and cs.noise_false_pass.total == 5

    assert len(result.extraction) == 17  # every document whose key class isn't noise
    es = result.extraction_summary
    for kind in (es.terms, es.invoice_facts, es.contacts):
        assert all(r == 1.0 for r in _rates(kind))
    assert es.terms.overall.expected == 31  # 29 key terms, plus 01's two again in the forward
    assert es.finding_coverage == {f"F{i}": [] for i in range(1, 6)}
    for d in result.extraction:
        assert not any(d.kind(k).missed or d.kind(k).extra for k in ("terms", "invoice_facts"))


def test_duplicates_score_against_their_originals():
    result = run(FX, docs=[FORWARD, RESYNC])
    forward, resync = result.extraction
    assert forward.duplicate_of == LUMENPOST
    assert len(forward.terms.matched) == 2 and len(forward.contacts.matched) == 1
    assert resync.duplicate_of == "inbox/06_kestrel_invoice_2026_10.eml"
    assert len(resync.invoice_facts.matched) == 1
    # Coverage counts only an item's own document, so the duplicates alone cover nothing.
    assert result.extraction_summary.finding_coverage == {}


def test_eval_fixture_extractor_leaves_fixture_extractor_alone():
    doc = FX.document_text(FORWARD, classified=True)
    assert FixtureExtractor(FX).extract(doc).terms == []  # D2: the worker relies on this
    ex = EvalFixtureExtractor(FX).extract(doc)
    assert {t.document for t in ex.terms} == {FORWARD}
    assert {c.document for c in ex.contacts} == {FORWARD}


def test_stub_engines_score_nothing():
    result = run(FX, classifier="stub", extractor="stub")
    assert result.classification_summary.noise_false_pass.rate() == 1.0  # all "contract"
    terms = result.extraction_summary.terms.overall
    assert terms.matched == 0 and terms.predicted == 0 and terms.expected == 31
    assert all(missing for missing in result.extraction_summary.finding_coverage.values())


def test_wrong_value_is_a_wrong_field_not_a_miss():
    ex = _corvana()
    price = next(t for t in ex.terms if t.id == "crv_price")
    price.value.amount = Decimal("12000")
    d = _score(CORVANA, ex)
    assert not d.terms.missed and not d.terms.extra
    match = next(m for m in d.terms.matched if m.key_id == "crv_price")
    wrong = [f for f in match.fields if not f.ok]
    assert [(f.field, f.expected, f.predicted) for f in wrong] == [
        ("value.amount", "14400", "12000")
    ]
    assert "wrong  crv_price.value.amount: 14400 ≠ 12000" in format_report(
        run(FX, docs=[CORVANA]).model_copy(update={"extraction": [d]})
    )


def test_dropped_term_is_a_miss_and_breaks_coverage():
    ex = _corvana()
    ex.terms = [t for t in ex.terms if t.id != "crv_notice"]
    d = _score(CORVANA, ex)
    assert [(m.key_id, m.type) for m in d.terms.missed] == [("crv_notice", "notice_window")]
    assert summarize_extraction(FX, [d]).finding_coverage == {"F1": ["crv_notice"]}


def test_invented_term_is_extra_with_quote_not_found():
    ex = _corvana()
    fake = ex.terms[0].model_copy(update={"id": "fake", "quotes": ["Not in the contract."]})
    ex.terms.append(fake)
    d = _score(CORVANA, ex)
    assert len(d.terms.matched) == 4
    assert [(x.pred_id, x.quote_found) for x in d.terms.extra] == [("fake", False)]


def test_vendor_alias_resolves():
    ex = _corvana()
    ex.terms = [t.model_copy(update={"vendor": "Corvana"}) for t in ex.terms]
    d = _score(CORVANA, ex)
    assert all(f.ok for m in d.terms.matched for f in m.fields if f.field == "vendor")

    ex.terms = [t.model_copy(update={"vendor": "Unknown Corp"}) for t in ex.terms]
    d = _score(CORVANA, ex)
    assert not any(f.ok for m in d.terms.matched for f in m.fields if f.field == "vendor")


def test_same_type_terms_pair_by_agreement_in_any_order():
    ex = FixtureExtractor(FX).extract(FX.document_text(LUMENPOST, classified=True))
    assert len(ex.terms) == 2 and {t.type for t in ex.terms} == {"price"}
    ex.terms.reverse()
    d = _score(LUMENPOST, ex)
    assert {(m.key_id, m.pred_id) for m in d.terms.matched} == {
        ("lp_growth_price_v2", "lp_growth_price_v2"),
        ("lp_addon_price_reaffirmed", "lp_addon_price_reaffirmed"),
    }
    assert all(f.ok for m in d.terms.matched for f in m.fields)


def test_engine_error_is_recorded_and_the_run_goes_on(monkeypatch):
    from leo.eval import runner

    class Broken:
        def __init__(self, fixture):
            pass

        def extract(self, doc):
            raise RuntimeError("boom")

    monkeypatch.setitem(runner.EXTRACTORS, "broken", Broken)
    result = run(FX, extractor="broken", docs=[CORVANA, LUMENPOST])
    assert [d.error for d in result.extraction] == ["RuntimeError: boom"] * 2
    assert result.extraction_summary.terms.overall.recall() == 0


def test_cli_writes_json(tmp_path, capsys):
    out = tmp_path / "eval.json"
    assert main(["--json", str(out)]) == 0
    assert "== Summary ==" in capsys.readouterr().out
    result = EvalResult.model_validate_json(out.read_text())
    assert result.classification_summary.accuracy.rate() == 1.0
    assert result.extraction_summary.terms.overall.recall() == 1.0


# --- Review fixes: pairing needs agreement, coverage needs right values, empty quotes ------


KESTREL_OCT = "inbox/06_kestrel_invoice_2026_10.eml"


def test_invented_invoice_fact_is_extra_and_the_real_one_missed():
    ex = FixtureExtractor(FX).extract(FX.document_text(KESTREL_OCT, classified=True))
    (fact,) = ex.invoice_facts
    ex.invoice_facts = [
        fact.model_copy(
            update={
                "id": "made_up",
                "amount": Decimal("999.99"),
                "invoice_date": fact.invoice_date.replace(year=2020),
                "invoice_number": "INV-FAKE",
                "billing_period": "2020-01",
                "quotes": ["Total due: $999.99"],
            }
        )
    ]
    d = _score(KESTREL_OCT, ex)
    assert d.invoice_facts.matched == []
    assert [x.pred_id for x in d.invoice_facts.extra] == ["made_up"]
    assert [x.key_id for x in d.invoice_facts.missed] == ["inv_kch_2610"]
    pr = summarize_extraction(FX, [d]).invoice_facts.overall
    assert pr.precision() == 0 and pr.recall() == 0


def test_invented_notice_window_is_extra_and_breaks_coverage():
    ex = _corvana()
    notice = next(t for t in ex.terms if t.id == "crv_notice")
    price = next(t for t in ex.terms if t.id == "crv_price")
    invented = notice.model_copy(
        update={
            "id": "wrong_clause",
            "value": notice.value.model_copy(
                update={"days": 7, "deliver_to": "nobody@example.com", "next_deadline": None}
            ),
            "quotes": price.quotes,  # a real quote, from a different clause
        }
    )
    ex.terms = [t for t in ex.terms if t.id != "crv_notice"] + [invented]
    d = _score(CORVANA, ex)
    assert [x.pred_id for x in d.terms.extra] == ["wrong_clause"]
    assert [x.key_id for x in d.terms.missed] == ["crv_notice"]
    assert summarize_extraction(FX, [d]).finding_coverage == {"F1": ["crv_notice"]}


def test_right_values_with_paraphrased_quote_still_match():
    ex = _corvana()
    price = next(t for t in ex.terms if t.id == "crv_price")
    price.quotes = ["Professional plan costs $14,400 a year."]
    d = _score(CORVANA, ex)
    match = next(m for m in d.terms.matched if m.key_id == "crv_price")
    assert not match.quote_found and not match.quote_matches_key
    assert all(f.ok for f in match.fields)


def test_wrong_value_breaks_coverage():
    ex = _corvana()
    notice = next(t for t in ex.terms if t.id == "crv_notice")
    notice.value.days = 90  # the key says 30
    d = _score(CORVANA, ex)
    match = next(m for m in d.terms.matched if m.key_id == "crv_notice")
    assert [f.field for f in match.fields if not f.ok] == ["value.days"]
    assert summarize_extraction(FX, [d]).finding_coverage == {"F1": ["crv_notice"]}


def test_empty_quote_is_not_found():
    ex = _corvana()
    blank = ex.terms[0].model_copy(update={"id": "blank", "quotes": [""]})
    ex.terms.append(blank)
    d = _score(CORVANA, ex)
    assert [(x.pred_id, x.quote_found) for x in d.terms.extra] == [("blank", False)]


def test_drafters_score_against_the_key():
    for name in ("fixture", "template"):
        result = run(FX, docs=[CORVANA], drafter=name)
        assert result.drafter == name
        assert [d.finding for d in result.drafts] == ["F1", "F2", "F3", "F4", "F5"]
        assert all(d.ok for d in result.drafts), name
        assert "drafts ok 5/5" in format_report(result)
    stub = run(FX, docs=[CORVANA], drafter="stub")
    assert not any(d.ok for d in stub.drafts)
    assert run(FX, docs=[CORVANA]).drafts == []


def test_script_extractor_skips_the_llm():
    result = run(FX, extractor="script")
    engines = {d.path: d.engine for d in result.extraction}
    assert engines.pop("inbox/05_kestrel_invoice_2026_09.eml") == "script:kestrel-invoice@1"
    assert engines.pop("inbox/06_kestrel_invoice_2026_10.eml") == "script:kestrel-invoice@1"
    assert engines.pop(RESYNC) == "script:kestrel-invoice@1"
    assert engines.pop("inbox/08_quillstack_invoice_2026_10.eml") == "script:quillstack-receipt@1"
    assert set(engines.values()) == {None}
    facts = result.extraction_summary.invoice_facts.overall
    assert (facts.matched, facts.predicted) == (4, 4)
    report = format_report(result)
    assert "reached the LLM                  0/17 documents" in report
