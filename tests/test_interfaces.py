"""The interfaces, stubs and clock between tracks (ROADMAP 0.5)."""

import csv
import datetime
import json
from pathlib import Path

import pytest

from leo import interfaces as i
from leo import stubs
from leo import types as t
from leo.clock import FixedClock, SystemClock, get_clock
from leo.config import Settings
from leo.rules import read_source_text

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"
KEY = json.loads((DATA / "expected.json").read_text())
KEY_ONLY_FINDING_FIELDS = {"days_until_deadline", "why_planted", "expected_action"}
DOC_PATH = "inbox/05_kestrel_invoice_2026_09.eml"


def key_ledger() -> i.Ledger:
    with (DATA / "transactions.csv").open(newline="") as f:
        transactions = list(csv.DictReader(f))
    fields = ("vendors", "documents", "terms", "invoice_facts")
    fields += ("transaction_vendor_map", "transaction_plan_map")
    return i.Ledger.model_validate({k: KEY[k] for k in fields} | {"transactions": transactions})


def doc_text(path: str = DOC_PATH) -> i.DocumentText:
    document = t.SourceDocument(path=path, doc_class=None, vendor=None)
    return i.DocumentText(document=document, text=read_source_text(DATA / path))


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


def run_chain(extractor: stubs.StubExtractor) -> tuple[i.Validated, i.Reconciliation]:
    """The worker's order (ROADMAP 2.1): classify, extract, resolve vendor, validate."""
    doc = doc_text()
    classification = stubs.StubClassifier().classify(doc)
    doc = doc.model_copy(
        update={"document": doc.document.model_copy(update={"doc_class": classification.doc_class})}
    )
    extraction = extractor.extract(doc)
    assert extraction is not None
    if extraction.vendor_name is not None:
        vendor_id = stubs.StubVendorResolver().vendor_for_name(extraction.vendor_name)
        extraction = extraction.with_vendor(vendor_id)
    validated = stubs.StubValidator().validate(doc, extraction, prior_terms=[])
    reconciliation = stubs.StubReconciler().reconcile(key_ledger(), datetime.date(2026, 11, 2))
    return validated, reconciliation


def test_stub_chain_without_vendor():
    validated, reconciliation = run_chain(stubs.StubExtractor())
    assert validated.extraction.vendor_name is None
    assert validated.issues == []
    assert reconciliation == i.Reconciliation()


def test_stub_chain_with_vendor():
    validated, _ = run_chain(stubs.StubExtractor(vendor_name="Kestrel Cloud"))
    assert validated.extraction.vendor_name == "Kestrel Cloud"


def test_vendor_resolver_stub_is_stable():
    resolver = stubs.StubVendorResolver()
    assert resolver.vendor_for_name("Kestrel Cloud") == "kestrel_cloud"
    assert resolver.vendor_for_name("Kestrel Cloud") == resolver.vendor_for_name("Kestrel Cloud")


def test_with_vendor_sets_vendor_only():
    term = t.extracted_term_adapter.validate_python(KEY["terms"][0])
    fact = t.InvoiceFact.model_validate(KEY["invoice_facts"][0])
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
    ledger = key_ledger()
    assert len(ledger.terms) == 29
    assert len(ledger.transactions) == 39
    assert ledger.transaction_plan_map["txn_0032"] == "Growth"


def test_invoice_fact_status_defaults_to_current():
    facts = [t.InvoiceFact.model_validate(x) for x in KEY["invoice_facts"]]
    assert {f.status for f in facts} == {t.TermStatus.current}


def test_stub_drafter_uses_vendor_contact():
    raw = next(f for f in KEY["findings"] if f["id"] == "F4")
    finding = t.finding_adapter.validate_python(
        {k: v for k, v in raw.items() if k not in KEY_ONLY_FINDING_FIELDS}
    )
    draft = stubs.StubDrafter().draft(finding, key_ledger())
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
