"""Natural-key builders. Pure: no database."""

from leo.db import keys
from leo.fixtures import DATA_DIR
from leo.rules import span_key
from leo.types import ActionType, FindingKind, TermType

SHA_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_document_keys():
    assert keys.eml_hash(b"") == f"v1:eml-hash:{SHA_EMPTY}"
    assert keys.file(b"") == f"v1:file:{SHA_EMPTY}"
    assert keys.gmail("acct1", "18c2f") == "v1:gmail:acct1:18c2f"
    assert keys.document_key("inbox/a.EML", b"") == keys.eml_hash(b"")
    assert keys.document_key("contracts/a.md", b"") == keys.file(b"")


def test_child_keys():
    doc = keys.file(b"")
    term = keys.term(doc, TermType.price, "10:20")
    assert term == f"v1:term:{doc}:price:10:20"
    assert keys.invoice_fact(doc, "5:9") == f"v1:invoice-fact:{doc}:5:9"
    txn = keys.txn("csv", "txn_0001")
    assert txn == "v1:txn:csv:txn_0001"
    finding = keys.finding(term, txn, FindingKind.price_cap_exceeded)
    assert finding == f"v1:finding:{term}:{txn}:price_cap_exceeded"
    assert keys.finding(term, None, FindingKind.auto_renewal_deadline).endswith(
        ":-:auto_renewal_deadline"
    )
    assert keys.job(doc, "extract") == f"v1:job:{doc}:extract"
    assert (
        keys.action(finding, ActionType.billing_dispute) == f"v1:action:{finding}:billing_dispute"
    )


def test_span_falls_back_to_quote_hash():
    assert span_key("abc price is $10 def", "price is $10") == "4:16"
    missing = span_key("abc", "not here")
    assert missing.startswith("q") and len(missing) == 17


def test_normalize_name():
    assert keys.normalize_name("Amazon Web Services, Inc.") == keys.normalize_name(
        "amazon web services inc"
    )
    assert keys.normalize_name("  Smith & Sons ") == "smith and sons"
    assert keys.normalize_name("Café Noir") == "café noir"


def test_d1_files_share_one_key():
    inbox = DATA_DIR / "inbox"
    a = (inbox / "06_kestrel_invoice_2026_10.eml").read_bytes()
    b = (inbox / "07_kestrel_invoice_2026_10_resynced.eml").read_bytes()
    assert keys.eml_hash(a) == keys.eml_hash(b)
