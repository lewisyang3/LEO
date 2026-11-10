"""The wired pipeline (ROADMAP 2.1): each stage's writes, the per-tenant reconcile, and a run
over the synthetic set.

Needs Postgres running and migrated. Every test works in its own tenant, inside the session's
rolled-back transaction. The synthetic run uses the committed extract cache, offline, with the
answer key's classes and the template drafter, so it needs no network.
"""

import datetime as dt
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from leo.clock import FixedClock
from leo.config import Settings
from leo.db import keys
from leo.db import models as m
from leo.db.read import load_findings, load_ledger
from leo.db.repo import Repo, upsert_tenant
from leo.e2e.checks import (
    anchor_of,
    check_rerun,
    key_anchor_of,
    run_checks,
    snapshot,
    stuck_jobs,
)
from leo.fixtures import load_fixture
from leo.ingest import folder
from leo.ingest.ingest import ingest
from leo.interfaces import DocumentText, Extraction, Ledger, Reconciliation
from leo.match.transactions import match_invoice_facts
from leo.pipeline.validate import RuleValidator
from leo.stubs import StubClassifier
from leo.types import DocumentClass, TermStatus, extracted_term_adapter
from leo.worker import queue
from leo.worker.pipeline import Pipeline
from leo.worker.run import Worker

TODAY = dt.date(2026, 11, 2)
CONTRACT = b"# Agreement\n\nAcme Corp. Fee: $339/month.\n"
ROOT = Path("data/synthetic")


@pytest.fixture
def repo(session):
    return Repo(session, upsert_tenant(session, f"pipeline-test-{uuid4().hex[:8]}", "Test"))


class OnePrice:
    """An extractor that finds one price, quoted from the document, under `vendor_name`."""

    def __init__(self, vendor_name: str = "Acme Corp.") -> None:
        self.vendor_name = vendor_name

    def extract(self, doc: DocumentText) -> Extraction:
        term = extracted_term_adapter.validate_python(
            {
                "id": "t1",
                "type": "price",
                "vendor": self.vendor_name,
                "document": doc.document.path,
                "quotes": ["Fee: $339/month."],
                "value": {"amount": "339", "currency": "USD", "per": "month"},
            }
        )
        return Extraction(vendor_name=self.vendor_name, terms=[term], engine="script:test@1")


def _pipeline(extractor=None) -> Pipeline:
    p = Pipeline.stubs()
    p.classifier = StubClassifier(DocumentClass.contract)
    p.extractors = [extractor or OnePrice()]
    p.validator = RuleValidator(FixedClock(TODAY))
    return p


def _worker(repo: Repo, raw_store, pipeline: Pipeline) -> Worker:
    return Worker(repo.session, pipeline, raw_store, FixedClock(TODAY), tenant_id=repo.tenant_id)


def _jobs(repo: Repo) -> list[m.Job]:
    repo.session.flush()
    q = select(m.Job).where(m.Job.tenant_id == repo.tenant_id).order_by(m.Job.created_at)
    return list(repo.session.scalars(q))


def _reconcile_job(repo: Repo) -> m.Job:
    (job,) = [j for j in _jobs(repo) if j.stage == queue.RECONCILE]
    return job


# --- Stage writes ----------------------------------------------------------------------------


def test_classify_and_validate_write_the_document_vendor_and_terms(repo, raw_store):
    doc_id = ingest(repo, raw_store, CONTRACT, "msa.md").document_id
    _worker(repo, raw_store, _pipeline()).drain()

    s = repo.session
    doc = s.get(m.SourceDocument, doc_id)
    assert doc.doc_class == "contract"
    vendor = s.get(m.Vendor, doc.vendor_id)
    assert vendor.canonical_name == "Acme Corp."
    (term,) = s.scalars(select(m.ContractTerm).where(m.ContractTerm.tenant_id == repo.tenant_id))
    assert (term.vendor_id, term.document_id, term.status) == (vendor.id, doc_id, "current")
    assert term.value["amount"] in ("339", "339.00", 339)
    (cite,) = s.scalars(select(m.Citation).where(m.Citation.term_id == term.id))
    assert CONTRACT.decode()[cite.start_offset : cite.end_offset] == "Fee: $339/month."
    assert term.natural_key == keys.term(
        doc.natural_key, "price", f"{cite.start_offset}:{cite.end_offset}"
    )


def test_a_failed_closed_document_stores_its_items_for_review_with_no_vendor(repo, raw_store):
    doc_id = ingest(repo, raw_store, CONTRACT, "msa.md").document_id
    _worker(repo, raw_store, _pipeline(OnePrice("Nobody Ltd."))).drain()

    s = repo.session
    assert s.get(m.SourceDocument, doc_id).vendor_id is None
    assert s.scalars(select(m.Vendor).where(m.Vendor.tenant_id == repo.tenant_id)).all() == []
    (term,) = s.scalars(select(m.ContractTerm).where(m.ContractTerm.tenant_id == repo.tenant_id))
    assert (term.vendor_id, term.status) == (None, TermStatus.needs_review)
    # The reconciler skips it, so nothing is found and nothing breaks.
    assert _reconcile_job(repo).status == "done"


def test_a_needs_review_invoice_fact_may_keep_a_period_that_does_not_parse(repo, raw_store):
    doc_id = ingest(repo, raw_store, CONTRACT, "msa.md").document_id
    fid = repo.upsert_invoice_fact(
        "v1:invoice-fact:test",
        vendor_id=None,
        document_id=doc_id,
        billing_period="last month",
        invoice_date=TODAY,
        amount=Decimal("1"),
        status="needs_review",
    )
    repo.session.flush()
    assert fid is not None


def test_a_fact_with_no_vendor_matches_no_charge(repo, raw_store):
    """A failed-closed fact has no vendor; a charge with no vendor isn't its match."""
    doc_id = ingest(repo, raw_store, CONTRACT, "msa.md").document_id
    fid = repo.upsert_invoice_fact(
        "v1:invoice-fact:test",
        vendor_id=None,
        document_id=doc_id,
        billing_period="2026-10",
        invoice_date=TODAY,
        amount=Decimal("339"),
        status="needs_review",
    )
    repo.upsert_transaction(
        "v1:txn:test:1",
        vendor_id=None,
        source="test",
        external_id="1",
        descriptor="UNKNOWN",
        card_last4="0000",
        amount=Decimal("339"),
        date=TODAY,
    )
    match_invoice_facts(repo)
    repo.session.flush()
    assert repo.session.get(m.InvoiceFact, fid).matches_transaction is None


# --- The per-tenant reconcile ----------------------------------------------------------------


def test_jobs_queued_in_one_transaction_run_in_the_order_they_were_queued(repo, raw_store):
    ids = [
        ingest(repo, raw_store, CONTRACT + bytes([65 + i]), f"msa{i}.md").document_id
        for i in range(5)
    ]
    assert [j.document_id for j in _jobs(repo)] == ids


@pytest.fixture
def claims(monkeypatch) -> list[str]:
    """The stage of every job the worker claims, in order. (`claimed_at` can't order them in
    a test: the whole test is one transaction, so `now()` doesn't move.)"""
    seen: list[str] = []
    real = queue.claim

    def claim(session, tenant_id=None):
        job = real(session, tenant_id)
        if job is not None:
            seen.append(job.stage)
        return job

    monkeypatch.setattr(queue, "claim", claim)
    return seen


def test_several_documents_get_one_reconcile_after_the_rest_are_through(repo, raw_store, claims):
    for i in range(3):
        ingest(repo, raw_store, CONTRACT + bytes([65 + i]), f"msa{i}.md")
    _worker(repo, raw_store, _pipeline()).drain()
    # 3 documents x 3 stages, then one reconcile, last.
    assert len(claims) == 10
    assert claims.count(queue.RECONCILE) == 1
    assert claims[-1] == queue.RECONCILE
    job = _reconcile_job(repo)
    assert (job.status, job.attempts, job.document_id) == ("done", 1, None)


def test_reconcile_waits_while_a_document_is_pending(repo, raw_store, session):
    ingest(repo, raw_store, CONTRACT, "msa.md")
    queue.request_reconcile(session, repo.tenant_id)
    job = queue.claim(session, repo.tenant_id)
    assert job.stage == queue.CLASSIFY  # not the older-looking reconcile
    session.rollback()


def test_a_new_document_requests_a_finished_reconcile_again(repo, raw_store, claims):
    ingest(repo, raw_store, CONTRACT, "msa.md")
    w = _worker(repo, raw_store, _pipeline())
    w.drain()
    assert claims.count(queue.RECONCILE) == 1

    ingest(repo, raw_store, CONTRACT + b"B", "msa2.md")
    w.drain()
    assert claims.count(queue.RECONCILE) == 2
    job = _reconcile_job(repo)
    assert (job.status, job.attempts) == ("done", 1)


class FailsOnce:
    """Classifies as a contract, after failing the first call with a transient error."""

    def __init__(self) -> None:
        self.calls = 0

    def classify(self, doc):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("Jev timed out")
        return StubClassifier(DocumentClass.contract).classify(doc)


def test_drain_waits_out_a_retry_so_the_reconcile_still_runs(repo, raw_store, claims):
    ingest(repo, raw_store, CONTRACT, "msa.md")
    p = _pipeline()
    p.classifier = FailsOnce()
    w = _worker(repo, raw_store, p)
    w.backoff = dt.timedelta(milliseconds=200)
    w.drain()
    assert claims == ["classify", "classify", "extract", "validate", "reconcile"]
    assert {j.status for j in _jobs(repo)} == {"done"}


@pytest.fixture
def deadline():
    """Fail a test that hangs (a drain that never returns) instead of blocking the run."""
    import signal

    def boom(*_):
        raise TimeoutError("drain did not return")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(10)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


def test_drain_does_not_spin_on_a_reconcile_blocked_by_a_running_job(
    repo, raw_store, session, deadline
):
    """A retried reconcile is due, but another worker is running one of the tenant's jobs: claim
    refuses the reconcile, so the drain must not wait for it (2.1 review)."""
    ingest(repo, raw_store, CONTRACT, "msa.md")
    queue.request_reconcile(session, repo.tenant_id)
    session.execute(
        update(m.Job)
        .where(m.Job.tenant_id == repo.tenant_id, m.Job.stage == queue.CLASSIFY)
        .values(status=queue.RUNNING, claimed_at=func.now(), attempts=1)
    )
    session.execute(
        update(m.Job)
        .where(m.Job.tenant_id == repo.tenant_id, m.Job.stage == queue.RECONCILE)
        .values(run_after=func.clock_timestamp() - dt.timedelta(seconds=1), attempts=1)
    )
    session.commit()
    assert queue.seconds_until_due(session, repo.tenant_id) is None
    assert _worker(repo, raw_store, _pipeline()).drain() == 0
    assert _reconcile_job(repo).status == queue.PENDING


def test_a_pending_request_is_left_alone(repo, session):
    queue.request_reconcile(session, repo.tenant_id)
    session.execute(update(m.Job).where(m.Job.tenant_id == repo.tenant_id).values(attempts=2))
    queue.request_reconcile(session, repo.tenant_id)
    assert _reconcile_job(repo).attempts == 2


# --- The synthetic set -----------------------------------------------------------------------


def _have_cache() -> bool:
    cache = Settings(_env_file=None).leo_llm_cache_dir / "extract"
    return any(cache.glob("*.json"))


needs_cache = pytest.mark.skipif(not _have_cache(), reason="no committed extraction cache")


@pytest.fixture
def synthetic(session, raw_store, monkeypatch):
    """The synthetic set ingested into a fresh tenant and drained, offline."""
    from leo.config import get_settings

    monkeypatch.setenv("LEO_LLM_OFFLINE", "1")
    get_settings.cache_clear()
    slug = f"pipeline-synthetic-{uuid4().hex[:8]}"
    tenant_id, new = folder.load(session, ROOT, slug, raw_store)
    session.commit()
    assert new == 21  # 22 paths; D1's resynced copy is the original's row
    pipeline = Pipeline.default(
        FixedClock(TODAY), classifier="fixture", drafter="template", sender="Brightwater"
    )
    w = Worker(session, pipeline, raw_store, FixedClock(TODAY), tenant_id=tenant_id)
    w.drain()
    return Repo(session, tenant_id), w


def _stuck(repo: Repo) -> list[tuple]:
    return stuck_jobs(repo.session, repo.tenant_id)


@needs_cache
def test_the_synthetic_set_gives_f1_to_f5_and_nothing_else(synthetic):
    repo, _ = synthetic
    s = repo.session
    assert _stuck(repo) == []
    fx = load_fixture()
    ledger = load_ledger(s, repo.tenant_id)
    found = load_findings(s, repo.tenant_id, ledger)
    # Kinds and anchors match the key's F1-F5 (by anchor term type and document, and the anchor
    # charge's date and amount); no near-miss (N1-N8) produces anything.
    assert sorted(anchor_of(f, ledger) for f in found) == sorted(
        key_anchor_of(f, fx) for f in fx.findings
    )
    for f in found:
        key = next(k for k in fx.findings if k.kind == f.kind)
        assert (f.deadline, f.recommended_send_by) == (key.deadline, key.recommended_send_by)
        assert f.impact.headline_amount == key.impact.headline_amount

    # D2: the forward is linked to its original; its terms count once.
    links = s.execute(
        select(m.DocumentLink.document_id, m.DocumentLink.linked_document_id).where(
            m.DocumentLink.tenant_id == repo.tenant_id,
            m.DocumentLink.link_type == "probable_duplicate",
        )
    ).all()
    paths = {
        d.id: d.path
        for d in s.scalars(
            select(m.SourceDocument).where(m.SourceDocument.tenant_id == repo.tenant_id)
        )
    }
    assert [(paths[a], paths[b]) for a, b in links] == [
        (
            "inbox/02_fw_lumenpost_price_change_notice.eml",
            "inbox/01_lumenpost_price_change_notice.eml",
        )
    ]
    # One supersession: Lumenpost Growth v1 -> v2.
    assert (
        s.scalar(
            select(func.count()).where(
                m.ContractTerm.tenant_id == repo.tenant_id, m.ContractTerm.status == "superseded"
            )
        )
        == 1
    )
    # One template draft per finding, with its engine recorded.
    actions = s.scalars(select(m.Action).where(m.Action.tenant_id == repo.tenant_id)).all()
    assert sorted(str(a.finding_id) for a in actions) == sorted(f.id for f in found)
    assert {a.raw_model_output["engine"] for a in actions} == {"template"}
    # ZapLedger never becomes a vendor; Sunridge and Metro come from their charges.
    names = set(
        s.scalars(select(m.Vendor.canonical_name).where(m.Vendor.tenant_id == repo.tenant_id))
    )
    assert not any("zap" in n.lower() for n in names)
    assert {"Sunridge Coffee Roasters", "Metro Water & Power"} <= names


def _snapshot(repo: Repo) -> dict[str, list]:
    return snapshot(repo.session, repo.tenant_id)


def _rerun_reconcile(repo: Repo, w: Worker) -> None:
    queue.request_reconcile(repo.session, repo.tenant_id)
    repo.session.commit()
    w.drain()
    assert _reconcile_job(repo).status == "done", _reconcile_job(repo).last_error


@needs_cache
def test_the_e2e_checks_pass_on_the_synthetic_set(synthetic):
    """`make e2e`'s checks (2.2, 2.3), offline: F1-F5 with their drafts, nothing extra, the vendor
    names, N1-N8 and D1-D5."""
    repo, w = synthetic
    s = repo.session
    checks = run_checks(s, repo.tenant_id, load_fixture())

    def rerun() -> int:
        _, new = folder.load(s, ROOT, s.get(m.Tenant, repo.tenant_id).slug, w.raw)
        queue.request_reconcile(s, repo.tenant_id)
        s.commit()
        w.drain()
        return new

    checks.append(check_rerun(s, repo.tenant_id, rerun))
    assert [c.id for c in checks] == [
        "jobs", *(f"F{i}" for i in range(1, 6)), "extra", "vendors",
        *(f"N{i}" for i in range(1, 9)), *(f"D{i}" for i in range(1, 6)),
    ]  # fmt: skip
    assert [(c.id, c.detail) for c in checks if not c.ok] == []


@needs_cache
def test_the_e2e_checks_catch_a_missing_finding_and_a_near_miss(synthetic):
    repo, _ = synthetic
    s = repo.session
    fx = load_fixture()
    ledger = load_ledger(s, repo.tenant_id)
    f1 = next(
        f for f in load_findings(s, repo.tenant_id, ledger) if f.kind == "auto_renewal_deadline"
    )
    s.execute(update(m.Finding).where(m.Finding.id == UUID(f1.id)).values(status="superseded"))
    # A Halcyon seat finding the key says must not exist (N7), anchored on F5's term.
    f5 = s.scalars(
        select(m.Finding).where(
            m.Finding.tenant_id == repo.tenant_id, m.Finding.kind == "escalator_applied_early"
        )
    ).one()
    Repo(s, repo.tenant_id).upsert_finding(
        keys.finding("n7-test", None, "seats_over_commitment"),
        vendor_id=f5.vendor_id,
        kind="seats_over_commitment",
        title="Halcyon seats",
        anchor_term_id=f5.anchor_term_id,
        impact={"overcharged_to_date": 0, "annualized": 0},
        computation={
            "contracted_seats": 50,
            "billed_seats": 51,
            "extra_seats": 1,
            "price_per_seat_month": 45,
            "overcharge_monthly": 45,
        },
    )
    s.flush()
    failed = {c.id for c in run_checks(s, repo.tenant_id, fx) if not c.ok}
    assert failed == {"F1", "extra", "N7"}


@needs_cache
def test_a_second_reconcile_round_changes_nothing(synthetic):
    repo, w = synthetic
    before = _snapshot(repo)
    _rerun_reconcile(repo, w)
    assert _snapshot(repo) == before


@needs_cache
def test_an_existing_action_is_never_redrafted(synthetic):
    repo, w = synthetic
    s = repo.session
    a = s.scalars(select(m.Action).where(m.Action.tenant_id == repo.tenant_id)).first()
    s.execute(
        update(m.Action).where(m.Action.id == a.id).values(body="edited", edited_at=func.now())
    )
    s.commit()
    _rerun_reconcile(repo, w)
    s.expire_all()
    assert s.get(m.Action, a.id).body == "edited"
    assert s.scalar(select(func.count()).where(m.Action.tenant_id == repo.tenant_id)) == 5


@needs_cache
def test_an_unedited_draft_follows_its_finding_and_a_decided_one_does_not(synthetic):
    """Each reconcile redrafts unedited drafts, so a recomputed finding's figures reach its
    draft; an approved action keeps what was approved (2.1 review)."""
    repo, w = synthetic
    s = repo.session
    first, second = s.scalars(
        select(m.Action).where(m.Action.tenant_id == repo.tenant_id).order_by(m.Action.id)
    ).all()[:2]
    body = first.body
    s.execute(update(m.Action).where(m.Action.id == first.id).values(body="stale"))
    s.execute(
        update(m.Action)
        .where(m.Action.id == second.id)
        .values(body="stale", state="approved", approver="reviewer", decided_at=func.now())
    )
    s.commit()
    _rerun_reconcile(repo, w)
    s.expire_all()
    assert s.get(m.Action, first.id).body == body
    assert s.get(m.Action, second.id).body == "stale"


def test_store_drafts_clears_the_drafters_records(repo, raw_store, monkeypatch):
    """A long-running worker must not keep every draft's attempt records."""
    from leo.types import ActionDraft
    from leo.worker.writes import store_drafts

    doc_id = ingest(repo, raw_store, CONTRACT, "msa.md").document_id
    _worker(repo, raw_store, _pipeline()).drain()
    term = repo.session.scalars(
        select(m.ContractTerm).where(m.ContractTerm.document_id == doc_id)
    ).one()
    nk = keys.finding(term.natural_key, None, "price_increase_notice")
    fid = repo.upsert_finding(
        nk,
        vendor_id=term.vendor_id,
        kind="price_increase_notice",
        title="T",
        anchor_term_id=term.id,
        impact={"amount": 1, "basis": "b"},
    )

    class Recorder:
        engine = "llm:test"

        def __init__(self):
            self.records = {}

        def draft(self, finding, ledger):
            self.records[finding.id] = {"attempts": [1]}
            return ActionDraft(
                finding=finding.id,
                type="negotiation_email",
                to="a@b.example",
                subject="S",
                body="B",
            )

    class F:
        id = nk
        kind = "price_increase_notice"

    drafter = Recorder()
    import leo.worker.writes as writes

    # The real draft_new needs a full Finding; this test is only about the records.
    monkeypatch.setattr(
        writes, "draft_new", lambda d, fs, ledger, existing: [d.draft(f, ledger) for f in fs]
    )
    store_drafts(repo, drafter, "llm:test", [F()], {nk: fid}, ledger=None)
    assert drafter.records == {}
    (a,) = repo.session.scalars(select(m.Action).where(m.Action.finding_id == fid))
    assert a.raw_model_output == {"attempts": [1], "engine": "llm:test"}


class Nothing:
    """A reconciler that finds nothing, as if every issue were fixed."""

    def reconcile(self, ledger: Ledger, today: dt.date) -> Reconciliation:
        return Reconciliation()


@needs_cache
def test_vanished_findings_are_superseded_and_come_back(synthetic):
    repo, w = synthetic
    s = repo.session
    real = w.pipeline.reconciler
    w.pipeline.reconciler = Nothing()
    _rerun_reconcile(repo, w)
    statuses = set(s.scalars(select(m.Finding.status).where(m.Finding.tenant_id == repo.tenant_id)))
    assert statuses == {"superseded"}
    # Supersessions are recomputed whole, so the Growth v1 term is current again.
    assert (
        s.scalar(
            select(func.count()).where(
                m.ContractTerm.tenant_id == repo.tenant_id,
                (m.ContractTerm.status == "superseded") | m.ContractTerm.superseded_by.is_not(None),
            )
        )
        == 0
    )
    assert load_findings(s, repo.tenant_id) == []

    w.pipeline.reconciler = real
    _rerun_reconcile(repo, w)
    assert len(load_findings(s, repo.tenant_id)) == 5
    assert s.scalar(select(func.count()).where(m.Action.tenant_id == repo.tenant_id)) == 5


def test_tenant_job_key():
    tid = UUID(int=1)
    assert keys.tenant_job(str(tid), "reconcile") == f"v1:job:tenant:{tid}:reconcile"
