"""The shared types accept the answer key as-is."""

import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from leo import types as t

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"
KEY = json.loads((DATA / "expected.json").read_text())

# Answer-key-only finding fields. The fixture loader owns these.
KEY_ONLY_FINDING_FIELDS = {"days_until_deadline", "why_planted", "expected_action"}

VALUE_CLASS = {
    "price": t.PriceValue,
    "price_cap": t.PriceCapValue,
    "escalator": t.EscalatorValue,
    "renewal": t.RenewalValue,
    "notice_window": t.NoticeWindowValue,
    "seat_count": t.SeatCountValue,
    "dispute_window": t.DisputeWindowValue,
}


def key_term(term_id):
    return next(x for x in KEY["terms"] if x["id"] == term_id)


def key_findings():
    return [
        {k: v for k, v in f.items() if k not in KEY_ONLY_FINDING_FIELDS} for f in KEY["findings"]
    ]


# --- Terms -----------------------------------------------------------------------------------


def test_all_terms_validate():
    terms = TypeAdapter(list[t.ExtractedTerm]).validate_python(KEY["terms"])
    assert len(terms) == 29
    for term in terms:
        assert isinstance(term.value, VALUE_CLASS[term.type])


def test_supersession_and_old_value():
    v1 = t.extracted_term_adapter.validate_python(key_term("lp_growth_price_v1"))
    v2 = t.extracted_term_adapter.validate_python(key_term("lp_growth_price_v2"))
    assert v1.status is t.TermStatus.superseded
    assert v1.superseded_by == "lp_growth_price_v2"
    assert v2.status is t.TermStatus.current
    assert v2.supersedes == "lp_growth_price_v1"
    assert v2.old_value == t.PriceValue(amount=Decimal("299"), currency="USD", per="month")


_DROP = object()


def _with_value(term_id, **changes):
    raw = json.loads(json.dumps(key_term(term_id)))
    for k, v in changes.items():
        if v is _DROP:
            del raw["value"][k]
        else:
            raw["value"][k] = v
    return raw


@pytest.mark.parametrize(
    "raw",
    [
        _with_value("crv_price", extra=1),  # extra key
        _with_value("crv_price", per=_DROP),  # missing key
        _with_value("crv_price", per="week"),  # bad enum
        _with_value("kch_dispute", next_deadline="2026-11-30"),  # notice-window key
    ],
    ids=["extra-key", "missing-key", "bad-enum", "dispute-with-next-deadline"],
)
def test_value_shape_rule_rejects(raw):
    with pytest.raises(ValidationError):
        t.extracted_term_adapter.validate_python(raw)


def test_null_value_field_passes():
    term = t.extracted_term_adapter.validate_python(_with_value("crv_price", amount=None))
    assert term.value.amount is None


def test_term_type_mismatch_rejected():
    raw = key_term("crv_price") | {"type": "price_cap"}
    with pytest.raises(ValidationError):
        t.extracted_term_adapter.validate_python(raw)


# --- Records ---------------------------------------------------------------------------------


def test_invoice_facts_validate():
    facts = [t.InvoiceFact.model_validate(x) for x in KEY["invoice_facts"]]
    assert len(facts) == 6
    assert next(f for f in facts if f.id == "inv_qs_2610").seats_billed == 32


def test_vendors_validate():
    vendors = [t.Vendor.model_validate(x) for x in KEY["vendors"]]
    quillstack = next(v for v in vendors if v.id == "quillstack")
    assert quillstack.contacts[0].email == "billing@quillstack.example"


def test_documents_validate():
    docs = [t.SourceDocument.model_validate(x) for x in KEY["documents"]]
    assert len(docs) == 22
    assert any(d.vendor is None for d in docs)
    assert {d.doc_class for d in docs} == set(t.DocumentClass)
    # Building by field name works too, and dumping by alias gives the key's shape back.
    doc = t.SourceDocument(path="x.eml", doc_class="noise", vendor=None)
    assert doc.model_dump(by_alias=True, exclude_defaults=True) == {
        "path": "x.eml",
        "class": "noise",
        "vendor": None,
    }


def test_transactions_csv_validates():
    with (DATA / "transactions.csv").open(newline="") as f:
        txns = [t.Transaction.model_validate(row) for row in csv.DictReader(f)]
    assert len(txns) == 39
    assert txns[0].seats_billed is None
    assert any(x.seats_billed is not None for x in txns)


# --- Findings --------------------------------------------------------------------------------


def test_findings_validate_as_their_kind():
    findings = TypeAdapter(list[t.Finding]).validate_python(key_findings())
    by_id = {f.id: f for f in findings}
    assert isinstance(by_id["F1"], t.AutoRenewalFinding)
    assert isinstance(by_id["F2"], t.PriceIncreaseFinding)
    assert isinstance(by_id["F3"], t.PriceCapExceededFinding)
    assert isinstance(by_id["F4"], t.SeatsOverCommitmentFinding)
    assert isinstance(by_id["F5"], t.EscalatorAppliedEarlyFinding)


def test_headline_amounts_match_totals():
    findings = {f.id: f for f in TypeAdapter(list[t.Finding]).validate_python(key_findings())}
    totals = KEY["totals"]
    assert findings["F1"].impact.headline_amount == Decimal(
        str(totals["renewal_decision_at_stake"])
    )
    annualized = sum(findings[i].impact.headline_amount for i in ("F2", "F3", "F4", "F5"))
    assert annualized == Decimal(str(totals["annualized_leak_excluding_renewal_decision"]))


def test_finding_rejects_key_only_fields():
    raw = KEY["findings"][0]
    with pytest.raises(ValidationError):
        t.finding_adapter.validate_python(raw)


# --- Enums -----------------------------------------------------------------------------------


def test_enums_cover_the_key():
    assert {f["kind"] for f in KEY["findings"]} == set(t.FindingKind)
    assert {d["class"] for d in KEY["documents"]} == set(t.DocumentClass)
    assert {x["type"] for x in KEY["terms"]} == set(t.TermType)
    for f in KEY["findings"]:
        action = f["expected_action"]
        t.ActionType(action["type"])
        if action.get("alternative"):
            t.ActionType(action["alternative"])


def test_action_draft_defaults():
    draft = t.ActionDraft(
        finding="F1",
        type="cancellation_notice",
        to="notices@corvana.example",
        subject="Non-renewal",
        body="...",
        alternative="renegotiation_request",
    )
    assert draft.state is t.ActionState.draft


# --- Serialization ---------------------------------------------------------------------------


def test_round_trip_and_money_is_a_json_number():
    for raw in KEY["terms"]:
        term = t.extracted_term_adapter.validate_python(raw)
        dumped = t.extracted_term_adapter.dump_python(term, mode="json")
        assert t.extracted_term_adapter.validate_python(dumped) == term
    for raw in key_findings():
        finding = t.finding_adapter.validate_python(raw)
        dumped = t.finding_adapter.dump_python(finding, mode="json")
        assert t.finding_adapter.validate_python(dumped) == finding

    price = t.extracted_term_adapter.validate_python(key_term("crv_price"))
    assert json.loads(price.model_dump_json())["value"]["amount"] == 14400.0


@pytest.mark.parametrize(
    "tp", [t.ExtractedTerm, t.Finding, t.ActionDraft, t.InvoiceFact, *VALUE_CLASS.values()]
)
def test_json_schema_builds(tp):
    assert TypeAdapter(tp).json_schema()
