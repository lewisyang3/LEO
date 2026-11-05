"""The interfaces, stubs and clock between tracks (ROADMAP 0.5)."""

import datetime

import pytest
from pydantic import ValidationError

from leo import interfaces as i
from leo import stubs
from leo import types as t
from leo.clock import FixedClock, SystemClock, get_clock
from leo.config import Settings
from leo.fixtures import load_fixture

FX = load_fixture()
DOC_PATH = "inbox/05_kestrel_invoice_2026_09.eml"


def doc_text(path: str = DOC_PATH) -> i.DocumentText:
    return FX.document_text(path)


@pytest.mark.parametrize(
    ("stub", "protocol"),
    [
        (stubs.StubClassifier(), i.Classifier),
        (stubs.StubExtractor(), i.Extractor),
        (stubs.StubValidator(), i.Validator),
        (stubs.StaticTransactionSource(), i.TransactionSource),
        (stubs.StubVendorResolver(), i.VendorResolver),
        (stubs.StubReconciler(), i.Reconciler),
        (stubs.StubDrafter(), i.Drafter),
        (FixedClock(datetime.date(2026, 11, 2)), i.Clock),
        (SystemClock(), i.Clock),
    ],
)
def test_stub_satisfies_protocol(stub, protocol):
    assert isinstance(stub, protocol)


def run_chain(
    extractor: stubs.StubExtractor, resolver: stubs.StubVendorResolver
) -> tuple[i.Validated, i.Reconciliation]:
    """The worker's order (ROADMAP 2.1): classify, extract, look up the vendor, validate,
    then create the vendor only if its name checks out."""
    doc = doc_text()
    classification = stubs.StubClassifier().classify(doc)
    doc = doc.model_copy(
        update={"document": doc.document.model_copy(update={"doc_class": classification.doc_class})}
    )
    extraction = extractor.extract(doc)
    assert extraction is not None
    if extraction.vendor_name is not None:
        assert resolver.find_vendor(extraction.vendor_name) is None  # unseen, so no prior terms
    validated = stubs.StubValidator().validate(doc, extraction, prior_terms=[])
    if validated.vendor_ok:
        vendor_id = resolver.vendor_for_name(validated.extraction.vendor_name)
        validated = validated.model_copy(
            update={"extraction": validated.extraction.with_vendor(vendor_id)}
        )
    reconciliation = stubs.StubReconciler().reconcile(FX.ledger(), datetime.date(2026, 11, 2))
    return validated, reconciliation


def test_stub_chain_without_vendor():
    resolver = stubs.StubVendorResolver()
    validated, reconciliation = run_chain(stubs.StubExtractor(), resolver)
    assert validated.extraction.vendor_name is None
    assert validated.vendor_ok is False
    assert resolver.vendors == set()
    assert reconciliation == i.Reconciliation()


def test_stub_chain_with_vendor_and_no_items():
    """Like the Harbor receipt: no terms or facts, but the vendor is still created."""
    resolver = stubs.StubVendorResolver()
    validated, _ = run_chain(stubs.StubExtractor(vendor_name="Kestrel Cloud"), resolver)
    assert validated.vendor_ok is True
    assert validated.extraction.terms == validated.extraction.invoice_facts == []
    assert resolver.find_vendor("Kestrel Cloud") == "kestrel_cloud"


def test_find_vendor_never_creates():
    resolver = stubs.StubVendorResolver()
    assert resolver.find_vendor("Kestrel Cloud") is None
    assert resolver.find_vendor("Kestrel Cloud") is None
    assert resolver.vendors == set()
    assert resolver.vendor_for_name("Kestrel Cloud") == "kestrel_cloud"
    assert resolver.vendor_for_name("Kestrel Cloud") == "kestrel_cloud"
    assert resolver.find_vendor("Kestrel Cloud") == "kestrel_cloud"


def test_vendor_from_descriptor_can_be_found():
    resolver = stubs.StubVendorResolver()
    vendor_id = resolver.vendor_for_descriptor("SUNRIDGE BAKERY")
    assert resolver.find_vendor("SUNRIDGE BAKERY") == vendor_id


def test_with_vendor_sets_vendor_only():
    term, fact = FX.terms[0], FX.invoice_facts[0]
    extraction = i.Extraction(
        vendor_name="Name As Written",
        terms=[term.model_copy(update={"vendor": "Name As Written"})],
        invoice_facts=[fact.model_copy(update={"vendor": "Name As Written"})],
        engine="test",
    )
    resolved = extraction.with_vendor("v1")
    assert [x.vendor for x in resolved.terms + resolved.invoice_facts] == ["v1", "v1"]
    assert resolved.terms[0].model_dump(exclude={"vendor"}) == term.model_dump(exclude={"vendor"})
    assert resolved.vendor_name == "Name As Written"
    assert extraction.terms[0].vendor == "Name As Written"  # the original is unchanged


def test_ledger_builds_from_the_key():
    ledger = FX.ledger()
    assert len(ledger.terms) == 29
    assert len(ledger.transactions) == 39
    assert ledger.transaction_plan_map["txn_0032"] == "Growth"


def test_invoice_fact_status_defaults_to_current():
    assert {f.status for f in FX.invoice_facts} == {t.TermStatus.current}


def test_stub_drafter_uses_vendor_contact():
    finding = next(f for f in FX.findings if f.id == "F4")
    draft = stubs.StubDrafter().draft(finding, FX.ledger())
    assert draft.to == "billing@quillstack.example"
    assert draft.state is t.ActionState.draft


def test_clock_uses_leo_today(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LEO_TODAY=2026-11-02\n")
    clock = get_clock(Settings(_env_file=env))
    assert clock.today() == datetime.date(2026, 11, 2)


def test_clock_falls_back_to_system_date(monkeypatch):
    monkeypatch.delenv("LEO_TODAY", raising=False)
    clock = get_clock(Settings(_env_file=None))
    assert isinstance(clock, SystemClock)


def test_vendor_ok_fails_closed():
    extraction = i.Extraction(vendor_name=None, engine="x")
    assert i.Validated(extraction=extraction, engine="x").vendor_ok is False
    with pytest.raises(ValidationError, match="vendor_ok requires vendor_name"):
        i.Validated(extraction=extraction, engine="x", vendor_ok=True)
