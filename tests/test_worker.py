"""The job queue and worker (A4).

Needs Postgres running and migrated (`make up && make migrate`). Every worker call is scoped to
the test's tenant, so jobs a dev worker or upload committed to the same database are untouched.
"""

import datetime as dt
from email.message import EmailMessage
from uuid import UUID

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from leo.clock import FixedClock
from leo.db import keys
from leo.db.models import Job, SourceDocument, Tenant
from leo.db.repo import Repo, upsert_tenant
from leo.db.seed import seed
from leo.db.session import get_engine
from leo.fixtures import load_fixture
from leo.ingest.ingest import ingest
from leo.interfaces import Classification, DocumentText, Extraction, Validated
from leo.llm import LLMOfflineMiss
from leo.stubs import STUB, StubClassifier, StubExtractor
from leo.types import DocumentClass
from leo.worker import queue
from leo.worker.pipeline import Pipeline
from leo.worker.run import Worker

TODAY = dt.date(2026, 11, 2)
CONTRACT = b"# Agreement\n\nAcme Corp. Fee: $339/month.\n"


@pytest.fixture
def repo(session):
    return Repo(session, upsert_tenant(session, "worker-test", "Worker test"))


def _worker(repo: Repo, raw_store, pipeline: Pipeline | None = None) -> Worker:
    return Worker(
        repo.session,
        pipeline or Pipeline.stubs(),
        raw_store,
        FixedClock(TODAY),
        tenant_id=repo.tenant_id,
    )


def _jobs(repo: Repo) -> list[Job]:
    repo.session.flush()
    q = select(Job).where(Job.tenant_id == repo.tenant_id).order_by(Job.created_at, Job.id)
    return list(repo.session.scalars(q))


def _by_stage(repo: Repo) -> dict[str, Job]:
    return {j.stage: j for j in _jobs(repo)}


def _due_now(repo: Repo) -> None:
    """Retries wait on `run_after`; tests skip the wait instead of sleeping."""
    repo.session.execute(update(Job).where(Job.tenant_id == repo.tenant_id).values(run_after=None))


def _ingest(repo: Repo, raw_store, raw: bytes = CONTRACT, name: str = "msa.md") -> UUID:
    return ingest(repo, raw_store, raw, name).document_id


class Flaky:
    """Raises `fails` times, then classifies as the stub does."""

    def __init__(self, fails: int, error: Exception | None = None) -> None:
        self.fails = fails
        self.error = error or ConnectionError("Jev timed out")
        self.calls = 0

    def classify(self, doc: DocumentText) -> Classification:
        self.calls += 1
        if self.calls <= self.fails:
            raise self.error
        return StubClassifier().classify(doc)


class NotMine:
    def extract(self, doc: DocumentText) -> Extraction | None:
        return None


class Recording:
    def __init__(self) -> None:
        self.seen: list[DocumentText] = []

    def extract(self, doc: DocumentText) -> Extraction | None:
        self.seen.append(doc)
        return Extraction(vendor_name="Acme Corp.", engine="script:acme@1")


# --- Enqueue ---------------------------------------------------------------------------------


def test_ingest_queues_one_classify_job_per_document(repo, raw_store):
    msg = EmailMessage()
    msg["Subject"] = "Signed"
    msg.set_content("See attached.")
    msg.add_attachment(CONTRACT, maintype="text", subtype="markdown", filename="msa.md")
    raw = msg.as_bytes()

    r = ingest(repo, raw_store, raw, "signed.eml")
    jobs = _jobs(repo)
    assert {j.document_id for j in jobs} == {r.document_id, *r.attachment_ids}
    assert {(j.stage, j.status, j.attempts) for j in jobs} == {("classify", "pending", 0)}
    email_key = keys.document_key("signed.eml", raw)
    assert keys.job(email_key, "classify") in {j.natural_key for j in jobs}

    ingest(repo, raw_store, raw, "signed.eml")
    assert len(_jobs(repo)) == 2


def test_reingest_does_not_requeue_a_done_job(repo, raw_store):
    _ingest(repo, raw_store)
    _worker(repo, raw_store).drain()
    before = [(j.id, j.status) for j in _jobs(repo)]
    _ingest(repo, raw_store)
    assert [(j.id, j.status) for j in _jobs(repo)] == before


def test_seed_queues_nothing(session):
    tenant = seed(session, load_fixture(), slug="worker-seed-test")
    session.flush()
    assert session.scalars(select(Job).where(Job.tenant_id == tenant)).all() == []


# --- The stage chain -------------------------------------------------------------------------


def test_drain_runs_every_stage_with_stubs(repo, raw_store):
    doc_id = _ingest(repo, raw_store)
    assert _worker(repo, raw_store).drain() == 4

    jobs = _by_stage(repo)
    assert list(jobs) == list(queue.STAGES)
    for j in jobs.values():
        assert (j.status, j.processed_by, j.attempts, j.last_error) == ("done", STUB, 1, None)
        assert j.claimed_at is not None
    assert {s: j.document_id for s, j in jobs.items()} == {
        "classify": doc_id,
        "extract": doc_id,
        "validate": doc_id,
        "reconcile": None,  # one per tenant (2.1)
    }
    assert jobs["reconcile"].natural_key == keys.tenant_job(str(repo.tenant_id), "reconcile")
    Classification.model_validate(jobs["classify"].raw_model_output)
    Extraction.model_validate(jobs["extract"].raw_model_output)
    Validated.model_validate(jobs["validate"].raw_model_output)
    out = jobs["reconcile"].raw_model_output
    assert out["today"] == TODAY.isoformat()
    assert (out["findings"], out["supersessions"], out["drafts"]) == ([], 0, [])
    # 2.1: classify writes the class to the row.
    assert repo.session.get(SourceDocument, doc_id).doc_class == "contract"


def test_noise_stops_after_classify(repo, raw_store):
    _ingest(repo, raw_store)
    pipeline = Pipeline.stubs()
    pipeline.classifier = StubClassifier(DocumentClass.noise)
    assert _worker(repo, raw_store, pipeline).drain() == 1
    assert [(j.stage, j.status) for j in _jobs(repo)] == [("classify", "done")]


def test_extract_gets_the_class_and_falls_through_to_the_next_extractor(repo, raw_store):
    _ingest(repo, raw_store)
    pipeline = Pipeline.stubs()
    pipeline.classifier = StubClassifier(DocumentClass.price_notice)
    script = Recording()
    pipeline.extractors = [NotMine(), script, StubExtractor()]
    _worker(repo, raw_store, pipeline).drain()

    assert [d.document.doc_class for d in script.seen] == [DocumentClass.price_notice]
    jobs = _by_stage(repo)
    assert jobs["extract"].processed_by == "script:acme@1"
    v = Validated.model_validate(jobs["validate"].raw_model_output)
    assert v.vendor_ok and v.extraction.vendor_name == "Acme Corp."


def test_no_extractor_matched_fails(repo, raw_store):
    _ingest(repo, raw_store)
    pipeline = Pipeline.stubs()
    pipeline.extractors = [NotMine()]
    _worker(repo, raw_store, pipeline).drain()
    jobs = _by_stage(repo)
    assert jobs["extract"].status == "failed"
    assert "no extractor matched" in jobs["extract"].last_error
    assert "validate" not in jobs


# --- Retries ---------------------------------------------------------------------------------


def test_transient_error_retries_then_succeeds(repo, raw_store):
    _ingest(repo, raw_store)
    pipeline = Pipeline.stubs()
    pipeline.classifier = Flaky(fails=2)
    w = _worker(repo, raw_store, pipeline)

    assert w.run_one()
    job = _by_stage(repo)["classify"]
    assert (job.status, job.attempts) == ("pending", 1)
    assert "ConnectionError" in job.last_error
    assert job.run_after is not None
    assert not w.run_one()  # waiting on run_after

    _due_now(repo)
    w.run_one()
    _due_now(repo)
    w.run_one()
    job = _by_stage(repo)["classify"]
    assert (job.status, job.attempts, job.last_error, job.run_after) == ("done", 3, None, None)


def test_out_of_retries_parks(repo, raw_store):
    _ingest(repo, raw_store)
    pipeline = Pipeline.stubs()
    pipeline.classifier = Flaky(fails=99)
    w = _worker(repo, raw_store, pipeline)
    for _ in range(3):
        _due_now(repo)
        assert w.run_one()
    _due_now(repo)
    assert not w.run_one()
    job = _by_stage(repo)["classify"]
    assert (job.status, job.attempts) == ("parked", 3)
    assert job.last_error
    assert pipeline.classifier.calls == 3


def test_non_retryable_error_fails_at_once(repo, raw_store):
    _ingest(repo, raw_store)
    pipeline = Pipeline.stubs()
    pipeline.classifier = Flaky(fails=99, error=LLMOfflineMiss("no cached answer"))
    _worker(repo, raw_store, pipeline).run_one()
    job = _by_stage(repo)["classify"]
    assert (job.status, job.attempts) == ("failed", 1)
    assert job.last_error.startswith("LLMOfflineMiss")


def _stale(job: Job, attempts: int) -> None:
    job.status = "running"
    job.attempts = attempts
    job.claimed_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)


def test_recover_stale_resets_old_running_jobs_and_parks_those_out_of_attempts(repo, raw_store):
    _ingest(repo, raw_store)
    _ingest(repo, raw_store, b"another contract", "other.md")
    _ingest(repo, raw_store, b"a third contract", "third.md")
    old, spent, fresh = _jobs(repo)
    _stale(old, 1)
    _stale(spent, 3)
    fresh.status = "running"
    fresh.claimed_at = dt.datetime.now(dt.UTC)
    repo.session.flush()

    assert queue.recover_stale(repo.session, tenant_id=repo.tenant_id) == 2
    for j in (old, spent, fresh):
        repo.session.refresh(j)
    assert (old.status, spent.status, fresh.status) == ("pending", "parked", "running")
    assert spent.last_error == queue.STOPPED
    assert old.last_error is None


class Interrupted:
    def classify(self, doc: DocumentText) -> Classification:
        raise KeyboardInterrupt


def test_interrupt_hands_the_claim_back(repo, raw_store):
    _ingest(repo, raw_store)
    pipeline = Pipeline.stubs()
    pipeline.classifier = Interrupted()
    with pytest.raises(KeyboardInterrupt):
        _worker(repo, raw_store, pipeline).run_one()
    job = _by_stage(repo)["classify"]
    repo.session.refresh(job)
    assert (job.status, job.attempts, job.claimed_at) == ("pending", 0, None)


class StopServe(Exception):
    pass


def test_serve_recovers_a_stale_job_while_idle(repo, raw_store, monkeypatch):
    """A job left `running` by a stopped worker comes back on the next idle poll."""
    _ingest(repo, raw_store)
    pipeline = Pipeline.stubs()
    pipeline.classifier = StubClassifier(DocumentClass.noise)
    w = _worker(repo, raw_store, pipeline)
    job = _by_stage(repo)["classify"]
    job.status = "running"  # claimed by nobody: nothing to run at startup
    job.claimed_at = dt.datetime.now(dt.UTC)
    repo.session.flush()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            _stale(job, 1)  # time passes: the claim is now old
            repo.session.flush()
        else:
            raise StopServe

    monkeypatch.setattr("leo.worker.run.time.sleep", sleep)
    with pytest.raises(StopServe):
        w.serve(poll_seconds=0)
    repo.session.refresh(job)
    assert (job.status, job.attempts) == ("done", 2)


# --- Claiming --------------------------------------------------------------------------------


def test_claim_is_fifo_and_scoped_to_the_tenant(repo, raw_store, session):
    first = _ingest(repo, raw_store)
    _ingest(repo, raw_store, b"another contract", "other.md")
    other = Repo(session, upsert_tenant(session, "worker-test-2", "Other"))
    _ingest(other, raw_store)

    job = queue.claim(session, repo.tenant_id)
    assert (job.document_id, job.status, job.attempts) == (first, "running", 1)
    assert queue.claim(session, other.tenant_id).tenant_id == other.tenant_id


def test_claim_skips_a_locked_row():
    """Two real connections, so the rows are committed under a throwaway tenant and deleted."""
    engine = get_engine()
    with Session(engine) as setup:
        tenant = upsert_tenant(setup, "worker-lock-test", "Lock test")
        doc = SourceDocument(
            tenant_id=tenant,
            natural_key="v1:file:lock-test",
            source_kind="file",
            path="uploads/lock.md",
            storage_path="lo/lock.md",
            filename="lock.md",
            content_sha256="0" * 64,
        )
        setup.add(doc)
        setup.flush()
        queue.enqueue(setup, tenant, doc.id, doc.natural_key, queue.CLASSIFY)
        setup.commit()
    try:
        with Session(engine) as a, Session(engine) as b:
            locked = a.scalars(select(Job).where(Job.tenant_id == tenant).with_for_update()).one()
            assert queue.claim(b, tenant) is None
            b.rollback()
            a.rollback()
            assert queue.claim(b, tenant).id == locked.id
            b.rollback()
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(text("SET CONSTRAINTS ALL DEFERRED"))
            cleanup.execute(delete(Job).where(Job.tenant_id == tenant))
            cleanup.execute(delete(SourceDocument).where(SourceDocument.tenant_id == tenant))
            cleanup.execute(delete(Tenant).where(Tenant.id == tenant))
            cleanup.commit()
