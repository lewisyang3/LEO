"""The feedback loop's corrections (ROADMAP Phase 4, F1): a reviewer corrects or rejects a term
or invoice fact, the event is recorded, re-extraction leaves the row alone, and the next
reconcile follows it.

Needs Postgres running and migrated. Each test works in its own tenant, inside the session's
rolled-back transaction.
"""

import datetime as dt
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from leo import feedback
from leo.api.app import app
from leo.api.routes import get_session
from leo.clock import FixedClock
from leo.config import Settings, get_settings
from leo.db import models as m
from leo.db.models import new_uuid7
from leo.db.read import load_findings, load_ledger, load_price_history
from leo.db.repo import Repo, upsert_tenant
from leo.db.seed import seed
from leo.db.write import write_term
from leo.ingest import folder
from leo.ingest.ingest import ingest
from leo.interfaces import DocumentText, Extraction
from leo.pipeline.validate import RuleValidator
from leo.stubs import StubClassifier
from leo.types import DocumentClass, FindingKind, TermStatus, TermType, extracted_term_adapter
from leo.worker import queue
from leo.worker.pipeline import Pipeline
from leo.worker.run import Worker

TODAY = dt.date(2026, 11, 2)
CONTRACT = b"# Agreement\n\nAcme Corp. Fee: $339/month.\n"
QUOTE = "Fee: $339/month."
ROOT = Path("data/synthetic")


def _term(path: str):
    return extracted_term_adapter.validate_python(
        {
            "id": "t1",
            "type": "price",
            "vendor": "Acme Corp.",
            "document": path,
            "quotes": [QUOTE],
            "value": {"amount": "339", "currency": "USD", "per": "month"},
        }
    )


class OnePrice:
    def extract(self, doc: DocumentText) -> Extraction:
        return Extraction(
            vendor_name="Acme Corp.", terms=[_term(doc.document.path)], engine="script:test@1"
        )


@pytest.fixture
def repo(session):
    return Repo(session, upsert_tenant(session, f"feedback-test-{uuid4().hex[:8]}", "Test"))


@pytest.fixture
def acme(repo, raw_store):
    """One contract with one price term, through the worker."""
    doc_id = ingest(repo, raw_store, CONTRACT, "msa.md").document_id
    p = Pipeline.stubs()
    p.classifier = StubClassifier(DocumentClass.contract)
    p.extractors = [OnePrice()]
    p.validator = RuleValidator(FixedClock(TODAY))
    w = Worker(repo.session, p, raw_store, FixedClock(TODAY), tenant_id=repo.tenant_id)
    w.drain()
    (term,) = repo.session.scalars(
        select(m.ContractTerm).where(m.ContractTerm.tenant_id == repo.tenant_id)
    )
    return doc_id, term


def _events(repo: Repo) -> list[m.FeedbackEvent]:
    q = select(m.FeedbackEvent).where(m.FeedbackEvent.tenant_id == repo.tenant_id)
    return list(repo.session.scalars(q.order_by(m.FeedbackEvent.id)))


def _reconcile(repo: Repo) -> m.Job:
    q = select(m.Job).where(m.Job.tenant_id == repo.tenant_id, m.Job.stage == queue.RECONCILE)
    return repo.session.scalar(q)


# --- Corrections -----------------------------------------------------------------------------


def test_a_correction_is_recorded_and_requests_a_reconcile(repo, acme):
    doc_id, term = acme
    assert _reconcile(repo).status == "done"
    events = feedback.correct_term(
        repo.session,
        repo.tenant_id,
        "reviewer",
        term.id,
        {"value": {"amount": "349"}, "plan_label": "Business"},
    )
    assert [e.field for e in events] == ["value.amount", "plan_label"]
    (ev,) = [e for e in _events(repo) if e.field == "value.amount"]
    assert (ev.event_type, ev.term_id, ev.document_id, ev.engine, ev.reviewer) == (
        "correct",
        term.id,
        doc_id,
        "script:test@1",
        "reviewer",
    )
    assert ev.old_value in ("339", "339.00", 339, 339.0) and ev.new_value in (349, 349.0)
    repo.session.refresh(term)
    assert term.corrected_at is not None and term.value["amount"] in (349, 349.0)
    assert term.plan_label == "Business"
    assert _reconcile(repo).status == "pending"


def test_re_extraction_never_overwrites_a_correction(repo, acme):
    doc_id, term = acme
    feedback.correct_term(
        repo.session, repo.tenant_id, "reviewer", term.id, {"value": {"amount": "349"}}
    )
    doc = repo.session.get(m.SourceDocument, doc_id)
    key, tid = write_term(repo, doc_id, doc.natural_key, CONTRACT.decode(), _term("msa.md"), None)
    repo.session.flush()
    assert tid == term.id
    repo.session.refresh(term)
    assert term.value["amount"] in (349, 349.0)
    assert term.vendor_id is not None  # the upsert didn't write the NULL vendor either
    (cite,) = repo.session.scalars(select(m.Citation).where(m.Citation.term_id == term.id))
    assert cite.quote == QUOTE


def test_nothing_changed_writes_nothing(repo, acme):
    _, term = acme
    assert (
        feedback.correct_term(
            repo.session, repo.tenant_id, "r", term.id, {"value": {"amount": "339"}}
        )
        == []
    )
    repo.session.refresh(term)
    assert term.corrected_at is None and _events(repo) == []


@pytest.mark.parametrize(
    "edits",
    [
        {"value": {"amount": "lots"}},
        {"value": {"per": "fortnight"}},
        {"value": {"color": "red"}},
        {"type": "renewal"},
        {"effective_date": "next week"},
    ],
)
def test_a_bad_correction_is_refused(repo, acme, edits):
    _, term = acme
    with pytest.raises(feedback.BadCorrection):
        feedback.correct_term(repo.session, repo.tenant_id, "r", term.id, edits)


def test_unknown_item_is_not_found(repo):
    with pytest.raises(feedback.ItemNotFound):
        feedback.correct_term(repo.session, repo.tenant_id, "r", new_uuid7(), {})


# --- Rejections ------------------------------------------------------------------------------


def test_a_rejected_term_leaves_the_price_history_and_stays_rejected(repo, acme):
    doc_id, term = acme
    vendor_id = term.vendor_id
    assert load_price_history(repo.session, repo.tenant_id, vendor_id)
    ev = feedback.reject(repo.session, repo.tenant_id, "r", "term", term.id)
    assert (ev.event_type, ev.old_value, ev.new_value) == ("reject", "current", "rejected")
    assert load_price_history(repo.session, repo.tenant_id, vendor_id) == []

    doc = repo.session.get(m.SourceDocument, doc_id)
    write_term(repo, doc_id, doc.natural_key, CONTRACT.decode(), _term("msa.md"), vendor_id)
    repo.session.flush()
    repo.session.refresh(term)
    assert term.status == TermStatus.rejected
    with pytest.raises(feedback.AlreadyRejected):
        feedback.reject(repo.session, repo.tenant_id, "r", "term", term.id)
    with pytest.raises(feedback.AlreadyRejected):
        feedback.correct_term(repo.session, repo.tenant_id, "r", term.id, {"plan_label": "x"})


def _fact(repo: Repo, doc_id: UUID, period: str, status: str) -> UUID:
    fid = repo.upsert_invoice_fact(
        f"v1:invoice-fact:{uuid4().hex}",
        vendor_id=None,
        document_id=doc_id,
        billing_period=period,
        invoice_date=TODAY,
        amount=Decimal("1"),
        status=status,
    )
    repo.session.flush()
    return fid


def test_a_fact_whose_period_does_not_parse_can_be_rejected_but_not_corrected_as_is(repo, acme):
    doc_id, _ = acme
    fid = _fact(repo, doc_id, "last month", "needs_review")
    with pytest.raises(feedback.BadCorrection):
        feedback.correct_invoice_fact(repo.session, repo.tenant_id, "r", fid, {"amount": "2"})
    feedback.reject(repo.session, repo.tenant_id, "r", "invoice_fact", fid)
    repo.session.flush()  # ck_invoice_period lets a rejected fact keep its period
    assert repo.session.get(m.InvoiceFact, fid).status == TermStatus.rejected


def test_correcting_a_flagged_fact_makes_it_current(repo, acme):
    doc_id, _ = acme
    fid = _fact(repo, doc_id, "last month", "needs_review")
    events = feedback.correct_invoice_fact(
        repo.session, repo.tenant_id, "r", fid, {"billing_period": "2026-10", "seats_billed": "12"}
    )
    assert sorted(e.field for e in events) == ["billing_period", "seats_billed"]
    row = repo.session.get(m.InvoiceFact, fid)
    assert (row.status, row.billing_period, row.seats_billed) == ("current", "2026-10", 12)


# --- The synthetic set: the next reconcile follows the reviewer ------------------------------


def _have_cache() -> bool:
    return any((Settings(_env_file=None).leo_llm_cache_dir / "extract").glob("*.json"))


needs_cache = pytest.mark.skipif(not _have_cache(), reason="no committed extraction cache")


@pytest.fixture
def synthetic(session, raw_store, monkeypatch):
    monkeypatch.setenv("LEO_LLM_OFFLINE", "1")
    get_settings.cache_clear()
    tenant_id, _ = folder.load(session, ROOT, f"feedback-{uuid4().hex[:8]}", raw_store)
    session.commit()
    pipeline = Pipeline.default(
        FixedClock(TODAY), classifier="fixture", drafter="template", sender="Brightwater"
    )
    w = Worker(session, pipeline, raw_store, FixedClock(TODAY), tenant_id=tenant_id)
    w.drain()
    return Repo(session, tenant_id), w


def _seats_finding(repo: Repo):
    findings = load_findings(
        repo.session, repo.tenant_id, load_ledger(repo.session, repo.tenant_id)
    )
    return [f for f in findings if f.kind == FindingKind.seats_over_commitment]


def _drain(repo: Repo, w: Worker) -> None:
    repo.session.commit()
    w.drain()
    assert _reconcile(repo).status == "done", _reconcile(repo).last_error


@needs_cache
def test_rejecting_the_seat_term_removes_the_finding(synthetic):
    repo, w = synthetic
    (f4,) = _seats_finding(repo)
    ledger = load_ledger(repo.session, repo.tenant_id)
    seat_terms = [t.id for t in ledger.terms if t.id in f4.terms and t.type == TermType.seat_count]
    assert seat_terms
    for tid in seat_terms:
        feedback.reject(repo.session, repo.tenant_id, "r", "term", UUID(tid))
    _drain(repo, w)
    assert _seats_finding(repo) == []


@needs_cache
def test_correcting_the_seat_count_moves_the_finding(synthetic):
    repo, w = synthetic
    (f4,) = _seats_finding(repo)
    before = f4.impact.headline_amount
    ledger = load_ledger(repo.session, repo.tenant_id)
    (seat,) = [t for t in ledger.terms if t.id in f4.terms and t.type == TermType.seat_count]
    feedback.correct_term(
        repo.session,
        repo.tenant_id,
        "r",
        UUID(seat.id),
        {"value": {"included": seat.value.included + 1}},
    )
    _drain(repo, w)
    after = _seats_finding(repo)
    assert not after or after[0].impact.headline_amount < before


# --- API -------------------------------------------------------------------------------------


@pytest.fixture
def client(session, raw_store, monkeypatch):
    slug = f"test-{new_uuid7()}"
    tenant_id = seed(session, slug=slug)
    session.commit()
    monkeypatch.setenv("LEO_TENANT", slug)
    monkeypatch.setenv("LEO_TODAY", "2026-11-02")
    get_settings.cache_clear()
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app), tenant_id
    app.dependency_overrides.clear()


def _f4(c: TestClient) -> dict:
    fid = next(
        r["id"] for r in c.get("/api/findings").json() if r["kind"] == "seats_over_commitment"
    )
    return c.get(f"/api/findings/{fid}").json()


def test_api_correct_and_reject(client, session):
    c, tenant_id = client
    detail = _f4(c)
    seat = next(t for t in detail["terms"] if t["term"]["type"] == "seat_count")
    assert seat["corrected"] is False
    tid = seat["term"]["id"]
    r = c.patch(f"/api/terms/{tid}", json={"value": {"included": 99}})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": tid,
        "kind": "term",
        "status": "current",
        "changed": ["value.included"],
    }
    assert next(t for t in _f4(c)["terms"] if t["term"]["id"] == tid)["corrected"] is True

    assert c.patch(f"/api/terms/{tid}", json={"value": {"included": "many"}}).status_code == 422
    assert c.patch(f"/api/terms/{tid}", json={"type": "price"}).status_code == 422
    assert c.patch(f"/api/terms/{new_uuid7()}", json={}).status_code == 404
    assert c.patch("/api/terms/nope", json={}).status_code == 404

    fact = detail["invoice_facts"][0]["fact"]["id"]
    r = c.post(f"/api/invoice-facts/{fact}/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert c.post(f"/api/invoice-facts/{fact}/reject").status_code == 409
    assert c.patch(f"/api/invoice-facts/{fact}", json={"amount": 1}).status_code == 409

    kinds = {
        e.event_type
        for e in session.scalars(
            select(m.FeedbackEvent).where(m.FeedbackEvent.tenant_id == tenant_id)
        )
    }
    assert kinds == {"correct", "reject"}


# --- Review fixes: rejected invoices claim no charge, reviewed documents keep their links --------


@needs_cache
def test_a_rejected_invoice_gives_up_its_charge(synthetic):
    repo, w = synthetic
    fact = repo.session.scalars(
        select(m.InvoiceFact)
        .where(
            m.InvoiceFact.tenant_id == repo.tenant_id,
            m.InvoiceFact.matches_transaction.is_not(None),
        )
        .order_by(m.InvoiceFact.id)
    ).first()
    assert fact is not None
    feedback.reject(repo.session, repo.tenant_id, "r", "invoice_fact", fact.id)
    _drain(repo, w)
    repo.session.refresh(fact)
    assert fact.matches_transaction is None


def _doc_id(repo: Repo, prefix: str) -> UUID:
    return repo.session.scalar(
        select(m.SourceDocument.id).where(
            m.SourceDocument.tenant_id == repo.tenant_id,
            m.SourceDocument.path.like(f"{prefix}%"),
        )
    )


def _price_notices(repo: Repo) -> list:
    ledger = load_ledger(repo.session, repo.tenant_id)
    findings = load_findings(repo.session, repo.tenant_id, ledger)
    return [f for f in findings if f.kind == FindingKind.price_increase_notice]


@needs_cache
def test_correcting_a_term_keeps_the_forward_linked(synthetic):
    """D2: 02 forwards 01. A correction on 01 changes its stored items, not the document, so
    the link holds and the forward's copy still doesn't count twice."""
    repo, w = synthetic
    original = _doc_id(repo, "inbox/01_")
    forward = _doc_id(repo, "inbox/02_")

    def linked() -> bool:
        return (
            repo.session.scalar(
                select(m.DocumentLink.id).where(
                    m.DocumentLink.tenant_id == repo.tenant_id,
                    m.DocumentLink.document_id == forward,
                    m.DocumentLink.linked_document_id == original,
                )
            )
            is not None
        )

    assert linked() and len(_price_notices(repo)) == 1
    prices = repo.session.scalars(
        select(m.ContractTerm).where(
            m.ContractTerm.tenant_id == repo.tenant_id,
            m.ContractTerm.document_id == original,
            m.ContractTerm.type == "price",
        )
    ).all()
    new_price = max(prices, key=lambda t: float(t.value["amount"]))  # the raised price
    amount = float(new_price.value["amount"]) + 10
    feedback.correct_term(
        repo.session, repo.tenant_id, "r", new_price.id, {"value": {"amount": amount}}
    )
    _drain(repo, w)
    assert linked()
    assert len(_price_notices(repo)) == 1


def test_a_value_correction_records_only_the_keys_sent(repo, acme):
    _, term = acme
    events = feedback.correct_term(
        repo.session, repo.tenant_id, "r", term.id, {"value": {"amount": "400"}}
    )
    assert [e.field for e in events] == ["value.amount"]
