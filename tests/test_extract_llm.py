"""LLM extraction (ROADMAP B3). No network: a fake client stands in for the API."""

import datetime
import json
from decimal import Decimal

import pytest
from anthropic.types import Message

from leo.clock import FixedClock
from leo.config import Settings
from leo.fixtures import load_fixture
from leo.interfaces import DocumentText
from leo.llm import LLMOfflineMiss, ResponseCache, json_schema_for
from leo.pipeline.extract_llm import (
    SCHEMAS,
    LLMExtractor,
    add_months,
    agreement_ref,
    labelled_ref,
    merge_parts,
    normalize_billing_period,
)
from leo.pipeline.extract_schema import EXTRACTION_PARTS, TERM_LISTS
from leo.types import SourceDocument, TermStatus

TODAY = datetime.date(2026, 11, 2)
D = datetime.date


def _doc(
    text: str = "Kestrel Cloud Hosting, LLC\nsome text",
    path: str = "contracts/x.md",
    doc_class: str = "contract",
):
    return DocumentText(
        document=SourceDocument(path=path, doc_class=doc_class, vendor=None), text=text
    )


def _grouped(out: dict) -> dict:
    """A test answer written with one `terms` list, in the model's per-type lists. The fake
    client sends the same answer to all three parts, and each part reads only its own lists."""
    if "terms" not in out:
        return out
    lists = {key: [] for key in TERM_LISTS}
    by_type = {t: key for key, t in TERM_LISTS.items()}
    for term in out["terms"]:
        item = {k: v for k, v in term.items() if k != "type"}
        lists[by_type.get(term.get("type"), "prices")].append(item)
    return {k: v for k, v in out.items() if k != "terms"} | lists


def _message(output, stop_reason: str = "end_turn") -> Message:
    text = output if isinstance(output, str) else json.dumps(_grouped(output))
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    )


class FakeMessages:
    """Replies with `reply`, or with `reply(kwargs)` when it is a function."""

    def __init__(self, reply) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def create(self, **kwargs) -> Message:
        self.calls.append(kwargs)
        return self.reply(kwargs) if callable(self.reply) else self.reply


class FakeClient:
    def __init__(self, reply: Message) -> None:
        self.messages = FakeMessages(reply)


def _extractor(tmp_path, reply=None, offline=False, model="claude-haiku-5-5"):
    client = FakeClient(reply or _message(_empty()))
    settings = Settings(
        _env_file=None, leo_llm_offline=offline, leo_model_extract=model, anthropic_api_key="x"
    )
    ex = LLMExtractor(
        clock=FixedClock(TODAY),
        cache=ResponseCache(tmp_path),
        client=client,  # type: ignore[arg-type]
        settings=settings,
    )
    return ex, client


def _empty(vendor="Kestrel Cloud Hosting, LLC"):
    return {"vendor_name": vendor, "terms": [], "invoice_facts": [], "contacts": []}


def _term(type_, value, quotes=("q",), plan_label=None, effective_date=None, **extra):
    return {
        "quotes": list(quotes),
        "plan_label": plan_label,
        "agreement_ref": None,
        "effective_date": effective_date,
        "type": type_,
        "value": value,
        **extra,
    }


def _renewal(**kw):
    v = {
        "auto_renew": None,
        "initial_term_end": None,
        "term_start": None,
        "initial_term_months": None,
        "renewal_length_months": None,
        "renewal_pricing": None,
        "early_termination_fee": None,
    }
    return _term("renewal", {**v, **kw}, effective_date="2024-09-01", plan_label="Plan")


def _notice(days, relative_to, applies_to="non_renewal"):
    return _term(
        "notice_window",
        {"days": days, "relative_to": relative_to, "applies_to": applies_to, "deliver_to": None},
        effective_date="2024-09-01",
        plan_label="Plan",
    )


def _price(amount, plan=None, old=None, per="month"):
    p = {"amount": amount, "currency": "USD", "per": per}
    old_value = None if old is None else {"amount": old, "currency": "USD", "per": per}
    return _term("price", p, plan_label=plan, effective_date="2026-12-01", old_value=old_value)


def _run(tmp_path, output, text="Kestrel Cloud Hosting, LLC\nsome text"):
    ex, _ = _extractor(tmp_path, _message(output))
    return ex.extract(_doc(text))


# --- Schema ----------------------------------------------------------------------------------


def test_parts_cover_every_list_once_and_require_everything():
    keys = [k for model, _ in EXTRACTION_PARTS.values() for k in model.model_fields]
    lists = [k for k in keys if k != "vendor_name"]
    assert sorted(lists) == sorted([*TERM_LISTS, "invoice_facts", "contacts"])
    for part, schema in SCHEMAS.items():
        assert "const" not in json.dumps(schema), part
        for name, d in {"root": schema, **schema.get("$defs", {})}.items():
            if d.get("type") == "object":
                assert set(d["required"]) == set(d["properties"]), (part, name)
                assert d["additionalProperties"] is False


def test_const_becomes_enum():
    from leo.pipeline.extract_schema import LLMPriceTerm

    schema = json_schema_for(LLMPriceTerm)
    assert schema["properties"]["type"]["enum"] == ["price"]
    assert "const" not in json.dumps(schema)


# --- Mapping ---------------------------------------------------------------------------------


def test_maps_to_shared_types(tmp_path):
    out = _empty()
    out["terms"] = [_price(339.0, plan="Growth", old=299.0)]
    out["invoice_facts"] = [
        {
            "quotes": ["Total: $1,308.00"],
            "invoice_number": "INV-1",
            "billing_period": "2026-09-01/2026-09-30",
            "invoice_date": "2026-09-01",
            "amount": 1308.0,
            "seats_billed": None,
        }
    ]
    out["contacts"] = [{"email": "billing@k.example", "purpose": "billing", "quote": "q"}]
    ex = _run(tmp_path, out)

    assert ex.vendor_name == "Kestrel Cloud Hosting, LLC"
    assert ex.engine == "llm:claude-haiku-5-5"
    [t] = ex.terms
    assert (t.id, t.vendor, t.document) == ("t1", "Kestrel Cloud Hosting, LLC", "contracts/x.md")
    assert t.value.amount == Decimal("339.0") and t.old_value.amount == Decimal("299.0")
    assert t.plan_label == "Growth" and t.effective_date == D(2026, 12, 1)
    assert t.status == TermStatus.current
    [f] = ex.invoice_facts
    assert (f.id, f.billing_period, f.amount) == ("f1", "2026-09", Decimal("1308.0"))
    [c] = ex.contacts
    assert c.document == "contracts/x.md"
    parts = ex.raw_output["parts"]
    assert list(parts) == list(EXTRACTION_PARTS)
    assert all(r["stop_reason"] == "end_turn" and r["output"] for r in parts.values())


def test_field_fixes_follow_the_key_conventions(tmp_path):
    out = _empty()
    out["terms"] = [_renewal(auto_renew=True), _notice(30, "term_end")]
    ex = _run(tmp_path, out)
    renewal, notice = ex.terms
    assert renewal.plan_label is None  # only price and seat_count carry a plan
    assert renewal.effective_date == D(2024, 9, 1)
    assert notice.plan_label is None and notice.effective_date is None


def test_bad_item_is_dropped_and_recorded(tmp_path):
    out = _empty()
    out["terms"] = [_price(10.0, plan="A"), {"type": "price", "value": "nope"}]
    out["terms"].append(_price(5.0, plan="B", old=None) | {"quotes": []})  # no quote
    ex = _run(tmp_path, out)
    assert [t.plan_label for t in ex.terms] == ["A"]
    assert len(ex.raw_output["item_errors"]) == 2


# --- Derived dates ---------------------------------------------------------------------------


def test_kestrel_term_from_length_rolls_forward_and_sets_the_deadline(tmp_path):
    out = _empty()
    out["terms"] = [
        _renewal(
            auto_renew=True,
            term_start="2024-09-01",
            initial_term_months=24,
            renewal_length_months=12,
        ),
        _notice(60, "term_end"),
    ]
    renewal, notice = _run(tmp_path, out).terms
    assert renewal.value.initial_term_end == D(2026, 8, 31)
    assert renewal.value.current_term_end == D(2027, 8, 31)
    assert notice.value.next_deadline == D(2027, 7, 2)


def test_corvana_term_not_yet_ended_is_not_rolled(tmp_path):
    out = _empty()
    out["terms"] = [
        _renewal(auto_renew=True, initial_term_end="2026-12-15", renewal_length_months=12),
        _notice(30, "term_end"),
    ]
    renewal, notice = _run(tmp_path, out).terms
    assert renewal.value.current_term_end == D(2026, 12, 15)
    assert notice.value.next_deadline == D(2026, 11, 15)


def test_driftwell_deadline_counts_from_the_renewal_date(tmp_path):
    out = _empty()
    out["terms"] = [
        _renewal(auto_renew=True, initial_term_end="2026-06-30", renewal_length_months=12),
        _notice(30, "renewal_date"),
    ]
    renewal, notice = _run(tmp_path, out).terms
    assert renewal.value.current_term_end == D(2027, 6, 30)
    assert notice.value.next_deadline == D(2027, 6, 1)


def test_non_auto_renewing_term_keeps_its_end(tmp_path):
    out = _empty()
    out["terms"] = [_renewal(term_start="2026-01-01", initial_term_months=36)]
    [renewal] = _run(tmp_path, out).terms
    assert renewal.value.initial_term_end == renewal.value.current_term_end == D(2028, 12, 31)


def test_price_change_notice_and_missing_end_have_no_deadline(tmp_path):
    out = _empty()
    out["terms"] = [
        _renewal(auto_renew=True, renewal_length_months=1),  # month to month, no end date
        _notice(30, "price_change", "vendor_price_change"),
        _notice(30, "term_end"),  # notice_deadline would raise: no current_term_end
    ]
    renewal, price_change, term_end = _run(tmp_path, out).terms
    assert renewal.value.current_term_end is None
    assert price_change.value.next_deadline is None
    assert term_end.value.next_deadline is None


def test_escalator_first_application_from_anniversary(tmp_path):
    out = _empty()
    value = {
        "pct": 3.0,
        "schedule": "each anniversary of the Effective Date",
        "first_application": None,
        "anniversary_base_date": "2026-01-01",
    }
    out["terms"] = [_term("escalator", value, effective_date="2026-01-01")]
    [esc] = _run(tmp_path, out).terms
    assert esc.value.first_application == D(2027, 1, 1)
    assert esc.value.pct == Decimal("3.0")


def test_out_of_range_numbers_never_crash(tmp_path):
    out = _empty()
    out["terms"] = [
        _renewal(term_start="2024-09-01", initial_term_months=10**6),  # year out of range
        _renewal(auto_renew=True, initial_term_end="2020-01-31", renewal_length_months=-1),
        _notice(10**6, "term_end"),  # deadline before year 1
    ]
    huge, backwards, notice = _run(tmp_path, out).terms
    assert huge.value.initial_term_end is None
    assert backwards.value.current_term_end == D(2020, 1, 31)  # not rolled
    assert notice.value.next_deadline is None


def test_add_months_clamps_to_month_end():
    assert add_months(D(2026, 1, 31), 1) == D(2026, 2, 28)
    assert add_months(D(2024, 9, 1), 24) == D(2026, 9, 1)


def test_billing_period_normalization():
    assert normalize_billing_period("2026-09-01/2026-09-30") == "2026-09"
    assert normalize_billing_period("2026-02-01/2026-02-28") == "2026-02"
    assert normalize_billing_period("2026-10-05/2026-11-04") == "2026-10-05/2026-11-04"
    assert normalize_billing_period("2026-07-01/2027-06-30") == "2026-07-01/2027-06-30"
    assert normalize_billing_period("2026-10") == "2026-10"
    assert normalize_billing_period(None) is None


# --- Cache and stop reasons ------------------------------------------------------------------


def test_cache_hit_makes_no_call(tmp_path):
    ex, client = _extractor(tmp_path)
    first = ex.extract(_doc())
    second = ex.extract(_doc())
    assert len(client.messages.calls) == 3  # one per part, then all from the cache
    assert first == second


def test_request_shape(tmp_path):
    ex, client = _extractor(tmp_path)
    ex.extract(_doc())
    calls = client.messages.calls
    assert len(calls) == 3
    assert len({json.dumps(c["output_config"]["format"]["schema"]) for c in calls}) == 3
    assert len({c["system"][0]["text"] for c in calls}) == 1  # one prompt
    for call in calls:
        assert call["model"] == "claude-haiku-5-5"
        assert call["output_config"]["effort"] == "medium"
        assert call["output_config"]["format"]["type"] == "json_schema"
        assert "tool_choice" not in call
        assert "contracts/x.md" not in call["messages"][0]["content"]  # no file-name hints


def test_cache_key_varies_with_model_and_prompt_version(tmp_path, monkeypatch):
    ex, client = _extractor(tmp_path)
    ex.extract(_doc())
    ex2, client2 = _extractor(tmp_path, model="claude-sonnet-5-5")
    ex2.extract(_doc())
    assert len(client2.messages.calls) == 3

    monkeypatch.setattr("leo.pipeline.extract_llm.PROMPT_VERSION", "extract-test")
    ex.extract(_doc())
    assert len(client.messages.calls) == 6


def test_cache_key_covers_the_user_prompt(tmp_path, monkeypatch):
    ex, client = _extractor(tmp_path)
    ex.extract(_doc())
    ex.extract(_doc(doc_class="invoice"))  # same text, classified differently
    assert len(client.messages.calls) == 6

    model, asks = EXTRACTION_PARTS["prices_renewals"]
    monkeypatch.setitem(EXTRACTION_PARTS, "prices_renewals", (model, asks + " (reworded)"))
    ex.extract(_doc())
    assert len(client.messages.calls) == 7  # only the reworded part is asked again


def test_offline_miss_raises_and_offline_hit_works(tmp_path):
    ex, _ = _extractor(tmp_path)
    ex.extract(_doc())
    offline, client = _extractor(tmp_path, offline=True)
    assert offline.extract(_doc()).vendor_name == "Kestrel Cloud Hosting, LLC"
    with pytest.raises(LLMOfflineMiss):
        offline.extract(_doc(text="another document"))
    assert client.messages.calls == []


@pytest.mark.parametrize(
    "reply",
    [
        _message("", stop_reason="refusal"),
        _message('{"vendor_name": "Kes', stop_reason="max_tokens"),
        _message("not json"),
        _message("[1, 2]"),
    ],
)
def test_incomplete_answer_is_empty_and_not_cached(tmp_path, reply):
    ex, client = _extractor(tmp_path, reply)
    out = ex.extract(_doc())
    assert out.vendor_name is None and not out.terms and not out.invoice_facts
    assert out.raw_output["error"]
    ex.extract(_doc())
    assert len(client.messages.calls) == 6  # retried, not cached
    assert not list(tmp_path.rglob("*.json"))


def test_one_failed_part_fails_closed_and_only_it_is_retried(tmp_path):
    def reply(kwargs):
        if "dispute_window" in kwargs["messages"][0]["content"]:
            return _message("", stop_reason="refusal")
        return _message(_empty())

    ex, client = _extractor(tmp_path, reply)
    out = ex.extract(_doc())
    assert out.vendor_name is None and not out.terms
    assert "windows_invoices_contacts" in out.raw_output["error"]
    assert len(list(tmp_path.rglob("*.json"))) == 2
    ex.extract(_doc())
    assert len(client.messages.calls) == 4  # only the failed part is asked again


def test_merge_takes_the_majority_vendor_and_each_parts_own_lists():
    a = {"vendor_name": "Lumenpost", "prices": [{"x": 1}], "renewals": [], "contacts": [{"c": 1}]}
    b = {"vendor_name": "Lumenpost Billing", "price_caps": [], "escalators": [], "seat_counts": []}
    c = {
        "vendor_name": "Lumenpost Billing",
        "notice_windows": [],
        "dispute_windows": [],
        "invoice_facts": [],
        "contacts": [],
    }
    merged, names = merge_parts(
        {"prices_renewals": a, "caps_escalators_seats": b, "windows_invoices_contacts": c}
    )
    assert merged["vendor_name"] == "Lumenpost Billing"
    assert names == ["Lumenpost", "Lumenpost Billing"]
    assert merged["terms"] == [{"x": 1, "type": "price"}]
    assert merged["contacts"] == []  # part a's stray contacts list isn't its to answer


# --- Eval regression on the committed cache --------------------------------------------------

FX = load_fixture()
F_DOCS = sorted({d for f in FX.findings for d in f.documents})


def _cached_eval():
    from leo.eval.runner import run

    settings = Settings(_env_file=None, leo_llm_offline=True)
    if not list((settings.leo_llm_cache_dir / "extract").glob("*.json")):
        pytest.skip("no committed extraction cache")
    import leo.eval.runner as runner

    factory = lambda fixture: LLMExtractor(  # noqa: E731
        clock=fixture.clock(), cache=ResponseCache(settings.leo_llm_cache_dir), settings=settings
    )
    runner.EXTRACTORS["llm-offline"] = factory
    try:
        return run(FX, extractor="llm-offline")
    finally:
        del runner.EXTRACTORS["llm-offline"]


def test_cached_extraction_covers_every_planted_finding():
    result = _cached_eval()
    assert all(d.error is None for d in result.extraction), [
        (d.path, d.error) for d in result.extraction if d.error
    ]
    es = result.extraction_summary
    assert es.finding_coverage == {f"F{i}": [] for i in range(1, 6)}
    assert es.terms.quote_found.rate() == 1.0
    assert es.invoice_facts.quote_found.rate() == 1.0


def test_agreement_ref_keeps_the_identifier_alone():
    assert agreement_ref("MSA No. KCH-MSA-2209") == "KCH-MSA-2209"
    assert agreement_ref("KCH-MSA-2209") == "KCH-MSA-2209"
    assert agreement_ref("Order Form QS-OF-1187") == "QS-OF-1187"
    assert agreement_ref("FL-77310-B") == "FL-77310-B"
    assert agreement_ref("Master Agreement") == "Master Agreement"
    assert agreement_ref("  ") is None
    assert agreement_ref(None) is None


def test_labelled_ref_finds_one_agreement_or_account_number():
    assert labelled_ref("Plan: Growth\nAccount ID: LP-204417\n") == "LP-204417"
    assert labelled_ref("Account No. FL-77310-B") == "FL-77310-B"
    assert labelled_ref("Agreement No. CRV-2025-0418 ... Agreement No. CRV-2025-0418") == (
        "CRV-2025-0418"
    )
    assert labelled_ref("Invoice No. INV-KCH-261001") is None  # an invoice isn't an agreement
    assert labelled_ref("Accounts Payable team") is None
    assert labelled_ref("Agreement No. A-1\nAccount ID: B-2") is None  # two ids: no guess


def test_terms_without_a_ref_take_the_documents_labelled_one(tmp_path):
    """2.3: Lumenpost writes "Account ID: LP-204417", which the model leaves out."""
    out = _empty("Lumenpost")
    out["terms"] = [
        _price(339, plan="Growth", old=299),
        {**_price(49, plan="Add-on", old=49), "agreement_ref": "Order No. ORD-9"},
    ]
    growth, addon = _run(tmp_path, out, text="Lumenpost\nAccount ID: LP-204417\n").terms
    assert growth.agreement_ref == "LP-204417"
    assert addon.agreement_ref == "ORD-9"  # the model's own ref is kept


def test_cached_terms_of_a_vendor_agree_on_their_agreement_ref():
    """The reconciler joins a vendor's terms on an exact agreement_ref (F3: the Kestrel cap and
    renewal). The model once wrote the cap's as "MSA No. KCH-MSA-2209" (2.1 review)."""
    settings = Settings(_env_file=None, leo_llm_offline=True)
    if not list((settings.leo_llm_cache_dir / "extract").glob("*.json")):
        pytest.skip("no committed extraction cache")
    ext = LLMExtractor(
        clock=FX.clock(), cache=ResponseCache(settings.leo_llm_cache_dir), settings=settings
    )
    refs: dict[str, set[str]] = {}
    for d in FX.documents:
        if d.doc_class == "noise" or d.vendor is None:
            continue
        ex = ext.extract(FX.document_text(d.path, classified=True))
        refs.setdefault(d.vendor, set()).update(
            t.agreement_ref for t in ex.terms if t.agreement_ref
        )
    assert {v: r for v, r in refs.items() if len(r) > 1} == {}
    assert refs["kestrel"] == {"KCH-MSA-2209"}


def test_cached_lumenpost_notice_keeps_the_add_on_separate():
    path = "inbox/01_lumenpost_price_change_notice.eml"
    settings = Settings(_env_file=None, leo_llm_offline=True)
    if not list((settings.leo_llm_cache_dir / "extract").glob("*.json")):
        pytest.skip("no committed extraction cache")
    ex = LLMExtractor(
        clock=FX.clock(), cache=ResponseCache(settings.leo_llm_cache_dir), settings=settings
    ).extract(FX.document_text(path, classified=True))
    prices = [t for t in ex.terms if t.type == "price"]
    assert len(prices) == 2
    assert len({t.plan_label for t in prices}) == 2


def test_cached_vendor_names_resolve():
    from leo.fixtures import FixtureVendorResolver

    settings = Settings(_env_file=None, leo_llm_offline=True)
    if not list((settings.leo_llm_cache_dir / "extract").glob("*.json")):
        pytest.skip("no committed extraction cache")
    resolver = FixtureVendorResolver(FX)
    ex = LLMExtractor(
        clock=FX.clock(), cache=ResponseCache(settings.leo_llm_cache_dir), settings=settings
    )
    for path in F_DOCS:
        name = ex.extract(FX.document_text(path, classified=True)).vendor_name
        assert name and resolver.find_vendor(name) == FX.document(path).vendor, (path, name)
