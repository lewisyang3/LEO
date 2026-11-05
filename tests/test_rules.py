"""The data-model rules agree with the answer key (ROADMAP 0.4)."""

import datetime
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

from leo import rules
from leo import types as t

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "synthetic"
KEY = json.loads((DATA / "expected.json").read_text())
TERMS = {x["id"]: x for x in KEY["terms"]}
FACTS = {x["id"]: x for x in KEY["invoice_facts"]}

sys.path.insert(0, str(ROOT / "scripts"))
from validate_answer_key import find_quote  # noqa: E402


def all_quotes():
    for group in ("terms", "invoice_facts"):
        for item in KEY[group]:
            for q in item["quotes"]:
                yield item["id"], item["document"], q
    for v in KEY["vendors"]:
        for c in v.get("contacts", []):
            yield f"{v['id']}:{c['email']}", c["document"], c["quote"]


@pytest.mark.parametrize("label,document,quote", list(all_quotes()))
def test_offsets_match_validator(label, document, quote):
    text = rules.read_source_text(DATA / document)
    count, start = find_quote(text, quote)
    assert count == 1, label
    assert rules.locate_quote(text, quote) == (start, start + len(quote))


def test_crlf_is_not_translated(tmp_path):
    f = tmp_path / "msg.eml"
    f.write_bytes(b"Subject: x\r\n\r\nPrice: $10\r\n")
    text = rules.read_source_text(f)
    assert rules.locate_quote(text, "Price: $10") == (14, 24)


def test_locate_quote_first_occurrence_and_missing():
    assert rules.locate_quote("ab ab", "ab") == (0, 2)
    assert rules.locate_quote("ab", "zz") is None


@pytest.mark.parametrize(
    "day,expected",
    [
        ("2026-11-13", "2026-11-13"),  # Friday
        ("2026-11-14", "2026-11-13"),  # Saturday
        ("2026-11-15", "2026-11-13"),  # Sunday (F1)
        ("2026-11-16", "2026-11-16"),  # Monday
    ],
)
def test_recommended_send_by(day, expected):
    d = datetime.date.fromisoformat
    assert rules.recommended_send_by(d(day)) == d(expected)


def deadline_from_basis(basis):
    terms = [TERMS[i] for i in basis["terms"]]
    by_type = {x["type"]: x for x in terms}
    if basis["invoice_fact"]:
        window = t.DisputeWindowValue(**by_type["dispute_window"]["value"])
        invoice_date = datetime.date.fromisoformat(FACTS[basis["invoice_fact"]]["invoice_date"])
        return rules.dispute_deadline(window, invoice_date)
    if "notice_window" in by_type:
        return rules.notice_deadline(
            t.NoticeWindowValue(**by_type["notice_window"]["value"]),
            t.RenewalValue(**by_type["renewal"]["value"]),
        )
    (price,) = terms
    return rules.price_change_deadline(datetime.date.fromisoformat(price["effective_date"]))


@pytest.mark.parametrize("finding", KEY["findings"], ids=lambda f: f["id"])
def test_finding_deadlines(finding):
    basis = finding["deadline_basis"]
    if basis is None:
        assert finding["deadline"] is None and finding["recommended_send_by"] is None
        return
    deadline = deadline_from_basis(basis)
    assert deadline.isoformat() == finding["deadline"]
    assert rules.recommended_send_by(deadline).isoformat() == finding["recommended_send_by"]


def first_span(item):
    return rules.locate_quote(rules.read_source_text(DATA / item["document"]), item["quotes"][0])


def test_span_keys_are_unique():
    spans = [first_span(x) for x in KEY["terms"] + KEY["invoice_facts"]]
    assert None not in spans
    term_keys = Counter((x["document"], x["type"], first_span(x)) for x in KEY["terms"])
    fact_keys = Counter((x["document"], first_span(x)) for x in KEY["invoice_facts"])
    assert max(term_keys.values()) == 1
    assert max(fact_keys.values()) == 1


def test_span_key():
    assert rules.span_key("ab cd", "cd") == "3:5"
    missing = rules.span_key("ab", "zz")
    assert missing.startswith("q") and len(missing) == 17
    assert rules.span_key("other text", "zz") == missing
    assert rules.span_key("ab", "yy") != missing


def test_notice_deadline_rejects_other_reference_points():
    notice = TERMS["lp_price_change_notice_rule"]["value"]
    renewal = t.RenewalValue(**TERMS["crv_renewal"]["value"])
    with pytest.raises(ValueError):
        rules.notice_deadline(t.NoticeWindowValue(**notice), renewal)
    with pytest.raises(ValueError):
        rules.notice_deadline(t.NoticeWindowValue(**{**notice, "relative_to": None}), renewal)


def test_dispute_deadline_rejects_other_reference_points():
    window = {**TERMS["kch_dispute"]["value"], "relative_to": "term_end"}
    with pytest.raises(ValueError):
        rules.dispute_deadline(t.DisputeWindowValue(**window), datetime.date(2026, 9, 1))
