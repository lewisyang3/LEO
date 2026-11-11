"""Stage 2 script extractors."""

import pytest

from leo.eval.scoring import score_extraction
from leo.fixtures import load_fixture
from leo.interfaces import DocumentText, Extraction
from leo.pipeline.extract_script import (
    KESTREL_INVOICE,
    QUILLSTACK_RECEIPT,
    ChainExtractor,
    script_extractor,
)
from leo.pipeline.validate import RuleValidator

FX = load_fixture()
KESTREL_09 = "inbox/05_kestrel_invoice_2026_09.eml"
KESTREL_10 = "inbox/06_kestrel_invoice_2026_10.eml"
KESTREL_RESYNC = "inbox/07_kestrel_invoice_2026_10_resynced.eml"
QUILLSTACK = "inbox/08_quillstack_invoice_2026_10.eml"
SCRIPTED = {
    KESTREL_09: "script:kestrel-invoice@1",
    KESTREL_10: "script:kestrel-invoice@1",
    KESTREL_RESYNC: "script:kestrel-invoice@1",
    QUILLSTACK: "script:quillstack-receipt@1",
}


def _doc(path: str) -> DocumentText:
    return FX.document_text(path, classified=True)


def _mangled(path: str, old: str, new: str) -> DocumentText:
    doc = _doc(path)
    assert old in doc.text
    return doc.model_copy(update={"text": doc.text.replace(old, new)})


class _Fallback:
    engine = "fallback"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def extract(self, doc: DocumentText) -> Extraction:
        self.seen.append(doc.document.path)
        return Extraction(vendor_name=None, engine=self.engine)


@pytest.mark.parametrize("path", [KESTREL_09, KESTREL_10, QUILLSTACK])
def test_script_output_matches_the_key(path):
    ex = script_extractor().extract(_doc(path))
    assert ex is not None
    assert ex.engine == SCRIPTED[path]
    assert ex.terms == [] and ex.contacts == []
    scored = score_extraction(FX, path, _doc(path).text, ex)
    facts = scored.invoice_facts
    assert len(facts.matched) == 1 and not facts.missed and not facts.extra
    m = facts.matched[0]
    assert all(f.ok for f in m.fields), [f for f in m.fields if not f.ok]
    assert m.quote_found and m.quote_matches_key


def test_matchers_pick_only_their_documents():
    kestrel = {d.path for d in FX.documents if KESTREL_INVOICE.matches(_doc(d.path))}
    quillstack = {d.path for d in FX.documents if QUILLSTACK_RECEIPT.matches(_doc(d.path))}
    assert kestrel == {KESTREL_09, KESTREL_10, KESTREL_RESYNC}
    assert quillstack == {QUILLSTACK}


def test_unmatched_documents_are_not_mine():
    scripts = script_extractor()
    for d in FX.documents:
        if d.path not in SCRIPTED:
            assert scripts.extract(_doc(d.path)) is None
    assert scripts.records == {}


@pytest.mark.parametrize("path", sorted(SCRIPTED))
def test_rule_validator_passes_script_output(path):
    ex = script_extractor().extract(_doc(path))
    assert ex is not None
    v = RuleValidator(FX.clock()).validate(_doc(path), ex, [])
    assert v.vendor_ok
    assert v.issues == []


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (KESTREL_09, "Total due: $1,308.00", "Balance: $1,308.00"),
        (KESTREL_09, "Total due: $1,308.00", "Total due: $1,400.00"),
        (QUILLSTACK, "32 seats x $18.00", "32 seats x $19.00"),
        (QUILLSTACK, "Service period:", "Period:"),
    ],
)
def test_changed_template_falls_through_to_the_next_extractor(path, old, new):
    doc = _mangled(path, old, new)
    scripts = script_extractor()
    assert scripts.extract(doc) is None
    assert scripts.records[path]["script"] == SCRIPTED[path]
    fallback = _Fallback()
    ex = ChainExtractor([scripts, fallback]).extract(doc)
    assert ex is not None and ex.engine == "fallback"
    assert fallback.seen == [path]


def test_chain_stops_at_the_first_answer():
    fallback = _Fallback()
    chain = ChainExtractor([script_extractor(), fallback])
    ex = chain.extract(_doc(QUILLSTACK))
    assert ex is not None and ex.engine == "script:quillstack-receipt@1"
    assert ex.raw_output is not None and ex.raw_output["version"] == "1"
    assert fallback.seen == []
    assert chain.extract(_doc("inbox/09_halcyon_invoice_2026_10.eml")).engine == "fallback"
