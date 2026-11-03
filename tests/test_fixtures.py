"""The fixture loader and fixture-backed stages (ROADMAP 0.6)."""

import datetime
import json

import pytest
from pydantic import ValidationError

from leo import interfaces as i
from leo import types as t
from leo.fixtures import (
    DATA_DIR,
    FixtureClassifier,
    FixtureDrafter,
    FixtureExtractor,
    FixtureReconciler,
    FixtureVendorResolver,
    load_fixture,
)
from leo.rules import locate_quote
from leo.stubs import StaticTransactionSource, StubValidator

FX = load_fixture()


def test_counts():
    assert len(FX.vendors) == 10
    assert len(FX.documents) == 22
    assert len(FX.terms) == 29
    assert len(FX.invoice_facts) == 6
    assert len(FX.transactions) == 39
    assert len(FX.findings) == 5
    assert len(FX.non_findings) == 8
    assert len(FX.dedup_checks) == 5


def test_key_only_fields_are_split_off():
    by_id = {x.finding.id: x for x in FX.expected_findings}
    assert FX.demo_today == datetime.date(2026, 11, 2)
    assert FX.clock().today() == FX.demo_today
    assert by_id["F1"].days_until_deadline == 13
    assert by_id["F4"].days_until_deadline is None
    assert by_id["F1"].expected_action.alternative is t.ActionType.renegotiation_request
    assert by_id["F4"].expected_action.pre_step is not None
    assert isinstance(by_id["F3"].finding, t.PriceCapExceededFinding)


def test_raw_key_finding_still_needs_the_split():
    raw = json.loads((DATA_DIR / "expected.json").read_text())["findings"][0]
    with pytest.raises(ValidationError):
        t.finding_adapter.validate_python(raw)


def test_each_load_is_a_fresh_copy():
    other = load_fixture()
    other.terms[0].status = t.TermStatus.superseded
    assert load_fixture().terms[0].status is t.TermStatus.current


def test_every_quote_is_located():
    items = [*FX.terms, *FX.invoice_facts]
    for item in items:
        text = FX.document_text(item.document).text
        for quote in item.quotes:
            assert locate_quote(text, quote) is not None, (item.id, quote)
    for vendor in FX.vendors:
        for contact in vendor.contacts:
            assert locate_quote(FX.document_text(contact.document).text, contact.quote)


def test_document_text_carries_no_answers():
    path = "inbox/02_fw_lumenpost_price_change_notice.eml"
    doc = FX.document_text(path).document
    assert doc.sha256 == FX.document(path).sha256
    assert doc.doc_class is doc.vendor is doc.probable_duplicate_of is doc.notes is None
    classified = FX.document_text(path, classified=True).document
    assert classified.doc_class is t.DocumentClass.price_notice
    assert classified.model_dump(exclude={"doc_class"}) == doc.model_dump(exclude={"doc_class"})


def test_ledger_and_findings_are_copies():
    ledger = FX.ledger()
    ledger.terms[0].status = t.TermStatus.superseded
    assert FX.terms[0].status is t.TermStatus.current
    result = FixtureReconciler(FX).reconcile(FX.ledger(), FX.demo_today)
    result.findings[0].title = "changed"
    assert FX.findings[0].title != "changed"


def test_sha256_flags_the_exact_duplicate_only():
    hashes = {d.path: d.sha256 for d in FX.documents}
    assert (
        hashes["inbox/06_kestrel_invoice_2026_10.eml"]
        == (hashes["inbox/07_kestrel_invoice_2026_10_resynced.eml"])
    )
    assert len(set(hashes.values())) == 21


@pytest.mark.parametrize(
    ("stage", "protocol"),
    [
        (FixtureClassifier(FX), i.Classifier),
        (FixtureExtractor(FX), i.Extractor),
        (StaticTransactionSource(FX.transactions), i.TransactionSource),
        (FixtureVendorResolver(FX), i.VendorResolver),
        (FixtureReconciler(FX), i.Reconciler),
        (FixtureDrafter(FX), i.Drafter),
    ],
)
def test_stage_satisfies_protocol(stage, protocol):
    assert isinstance(stage, protocol)


def test_chain_reproduces_the_key():
    """The 0.5 worker order over every non-noise document gives back the key's items."""
    classifier, extractor = FixtureClassifier(FX), FixtureExtractor(FX)
    resolver, validator = FixtureVendorResolver(FX), StubValidator()
    terms, facts = [], []
    for document in FX.documents:
        doc = FX.document_text(document.path)
        doc_class = classifier.classify(doc).doc_class
        assert doc_class == document.doc_class
        if doc_class is t.DocumentClass.noise:
            continue
        doc = FX.document_text(document.path, classified=True)
        extraction = extractor.extract(doc)
        assert extraction is not None
        if extraction.vendor_name is not None:
            assert resolver.find_vendor(extraction.vendor_name) == document.vendor
        validated = validator.validate(doc, extraction, prior_terms=[])
        assert validated.vendor_ok
        vendor_id = resolver.vendor_for_name(validated.extraction.vendor_name)
        assert vendor_id == document.vendor
        resolved = validated.extraction.with_vendor(vendor_id)
        terms += resolved.terms
        facts += resolved.invoice_facts
    assert sorted(terms, key=lambda x: x.id) == sorted(FX.terms, key=lambda x: x.id)
    assert sorted(facts, key=lambda x: x.id) == sorted(FX.invoice_facts, key=lambda x: x.id)


def test_extractor_gives_names_as_written():
    extraction = FixtureExtractor(FX).extract(
        FX.document_text("contracts/quillstack_order_form.md")
    )
    assert extraction.vendor_name == "Quillstack Inc."
    assert {x.vendor for x in extraction.terms} == {"Quillstack Inc."}
    assert [c.email for c in extraction.contacts] == ["billing@quillstack.example"]
    forward = FixtureExtractor(FX).extract(
        FX.document_text("inbox/02_fw_lumenpost_price_change_notice.eml")
    )
    assert forward.terms == forward.invoice_facts == []


def test_resolver_matches_descriptors_and_creates_unknowns():
    resolver = FixtureVendorResolver(FX)
    for txn in FX.transactions:
        assert resolver.vendor_for_descriptor(txn.descriptor) == (FX.transaction_vendor_map[txn.id])
    assert resolver.find_vendor("ZapLedger") is None
    assert resolver.find_vendor("ZapLedger") is None  # find never creates
    created = resolver.vendor_for_name("ZapLedger")
    assert resolver.find_vendor("ZapLedger") == created


def test_reconciler_returns_the_key():
    result = FixtureReconciler(FX).reconcile(FX.ledger(), FX.demo_today)
    assert [f.id for f in result.findings] == ["F1", "F2", "F3", "F4", "F5"]
    assert result.supersessions == [
        i.Supersession(old="lp_growth_price_v1", new="lp_growth_price_v2")
    ]


def test_drafts_include_the_required_facts():
    drafter, ledger = FixtureDrafter(FX), FX.ledger()
    for expected in FX.expected_findings:
        draft = drafter.draft(expected.finding, ledger)
        assert draft.type is expected.expected_action.type
        assert draft.to == expected.expected_action.to
        assert draft.state is t.ActionState.draft
        for s in expected.expected_action.must_include:
            assert s in draft.body
