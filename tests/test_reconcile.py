"""Reconciliation rules (ROADMAP C1), on the answer key's ledger."""

import datetime
from decimal import Decimal

import pytest

from leo.fixtures import load_fixture
from leo.interfaces import Reconciler, Supersession
from leo.reconcile.reconciler import RuleReconciler, price_on
from leo.types import FindingKind, PriceTerm, TermStatus

FX = load_fixture()
TODAY = FX.demo_today
EXPECTED = {x.finding.kind: x.finding for x in FX.expected_findings}
SET_FIELDS = ("terms", "transactions", "invoice_facts", "documents", "linked_duplicates")
EXACT_FIELDS = ("vendor", "plan_label", "anchor", "deadline", "recommended_send_by")


def _run(ledger=None, today=TODAY):
    return RuleReconciler().reconcile(ledger or FX.ledger(), today)


def _by_kind(result):
    return {f.kind: f for f in result.findings}


def _blank_statuses(ledger):
    for t in ledger.terms:
        t.status, t.superseded_by, t.supersedes = TermStatus.current, None, None
    return ledger


def _comparable(f) -> dict:
    out = {k: getattr(f, k) for k in EXACT_FIELDS}
    out |= {k: sorted(getattr(f, k)) for k in SET_FIELDS}
    b = f.deadline_basis
    out["deadline_basis"] = (sorted(b.terms), b.invoice_fact) if b else None
    out["impact"] = f.impact.model_dump(exclude={"basis"})
    out["computation"] = getattr(f, "computation", None)
    return out


def test_is_a_reconciler():
    assert isinstance(RuleReconciler(), Reconciler)


def test_one_finding_per_kind():
    assert sorted(f.kind for f in _run().findings) == sorted(FindingKind)


@pytest.mark.parametrize("kind", list(FindingKind), ids=lambda k: k.value)
def test_matches_key(kind):
    got = _by_kind(_run())[kind]
    assert _comparable(got) == _comparable(EXPECTED[kind])


def test_supersessions():
    assert _run().supersessions == [
        Supersession(old="lp_growth_price_v1", new="lp_growth_price_v2")
    ]


def test_status_blind():
    assert _run(_blank_statuses(FX.ledger())) == _run()


def test_ids_deterministic_and_ledger_untouched():
    ledger = FX.ledger()
    before = ledger.model_copy(deep=True)
    a, b = _run(ledger), _run(ledger)
    assert a == b
    assert ledger == before
    ids = [f.id for f in a.findings]
    assert len(set(ids)) == len(ids)
    f1 = _by_kind(a)[FindingKind.auto_renewal_deadline]
    assert f1.id == "v1:finding:crv_notice:-:auto_renewal_deadline"
    f3 = _by_kind(a)[FindingKind.price_cap_exceeded]
    assert f3.id == "v1:finding:kch_cap:txn_0025:price_cap_exceeded"


def test_near_misses_quiet():
    findings = _run().findings
    quiet_vendors = {"ferrolink", "driftwell", "harbor_supply", "sunridge", "metro_utility"}
    assert not [f for f in findings if f.vendor in quiet_vendors]
    quiet_terms = {"lp_addon_price_v1", "lp_addon_price_reaffirmed", "kch_notice", "hps_seats"}
    assert not [f for f in findings if quiet_terms & {f.anchor.term, *f.terms}]


def test_needs_review_notice_hides_f1():
    ledger = FX.ledger()
    next(t for t in ledger.terms if t.id == "crv_notice").status = TermStatus.needs_review
    assert FindingKind.auto_renewal_deadline not in _by_kind(_run(ledger))


@pytest.mark.parametrize(
    "today",
    [datetime.date(2026, 11, 16), datetime.date(2026, 9, 15)],
    ids=["deadline-passed", "beyond-lookahead"],
)
def test_f1_window(today):
    assert FindingKind.auto_renewal_deadline not in _by_kind(_run(today=today))


def test_price_on_effective_date():
    growth = [t for t in FX.terms if isinstance(t, PriceTerm) and t.plan_label == "Growth"]
    assert price_on(growth, datetime.date(2026, 11, 30)).id == "lp_growth_price_v1"
    assert price_on(growth, datetime.date(2026, 12, 1)).id == "lp_growth_price_v2"


def test_later_today_moves_dispute_deadline_and_drops_f2():
    found = _by_kind(_run(today=datetime.date(2026, 12, 1)))
    assert FindingKind.price_increase_notice not in found
    f3 = found[FindingKind.price_cap_exceeded]
    assert f3.deadline == datetime.date(2026, 12, 30)
    assert f3.deadline_basis.invoice_fact == "inv_kch_2610"


def test_weekend_note():
    f1 = _by_kind(_run())[FindingKind.auto_renewal_deadline]
    assert "Sunday" in f1.deadline_notes and "Friday" in f1.deadline_notes


@pytest.mark.parametrize(
    ("per", "amount"), [("seat_month", "18.00"), ("year", "14400.00")], ids=["seat", "year"]
)
def test_cap_ignores_non_monthly_price(per, amount):
    """F3 compares a monthly price with invoice totals, so any other `per` is skipped."""
    ledger = FX.ledger()
    price = next(t for t in ledger.terms if t.id == "kch_price")
    price.value = price.value.model_copy(update={"per": per, "amount": Decimal(amount)})
    assert FindingKind.price_cap_exceeded not in _by_kind(_run(ledger))


def test_cap_ignores_zero_price():
    """A $0 prior price has no percent increase, so F3 skips it instead of dividing by zero."""
    ledger = FX.ledger()
    price = next(t for t in ledger.terms if t.id == "kch_price")
    price.value = price.value.model_copy(update={"amount": Decimal("0")})
    assert FindingKind.price_cap_exceeded not in _by_kind(_run(ledger))
