"""The worker (ROADMAP A4): claim a job, run its stage, record the result, queue the next stage.

Stages, one job each per document: classify -> extract -> validate -> reconcile. A noise
document stops after classify. Each stage reads the previous stage's output from that job's
`raw_model_output`, so a retry reruns one stage, not the chain.

A4 writes only `job` rows: stage outputs live in `raw_model_output`, and `processed_by` is
the engine that produced them. Writing documents, terms, findings and actions through `Repo`
is 2.1, in the same transaction that marks the job done.

Failures: a non-retryable error marks the job `failed`. Any other error retries with backoff
(`run_after`), and after `max_attempts` the job is `parked` for a person. So a document whose
classification fails stays unclassified, never noise.
"""

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pydantic
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leo import stubs
from leo.db import keys
from leo.db.models import Job
from leo.db.models import SourceDocument as DocumentRow
from leo.db.read import load_ledger
from leo.ingest.store import RawStore
from leo.interfaces import Classification, Clock, DocumentText, Extraction
from leo.llm import LLMOfflineMiss
from leo.rules import read_source_text
from leo.types import DocumentClass, SourceDocument, TermStatus
from leo.worker import queue
from leo.worker.pipeline import Pipeline

log = logging.getLogger(__name__)

BACKOFF = dt.timedelta(seconds=2)  # doubled each retry: 2s, 4s, ...
NEXT_STAGE = {
    queue.CLASSIFY: queue.EXTRACT,
    queue.EXTRACT: queue.VALIDATE,
    queue.VALIDATE: queue.RECONCILE,
    queue.RECONCILE: None,
}


class Permanent(RuntimeError):
    """A stage error that retrying can't fix."""


NON_RETRYABLE: tuple[type[BaseException], ...] = (
    Permanent,
    LLMOfflineMiss,  # offline and not cached: a retry hits the same miss
    FileNotFoundError,  # the raw file is gone
    pydantic.ValidationError,  # a stage returned, or a job holds, output that doesn't parse
)


@dataclass(frozen=True)
class StageResult:
    output: dict[str, Any]
    processed_by: str
    next_stage: str | None


def _engine(component: object) -> str:
    """For components whose results carry no `engine` (the reconciler)."""
    engine = getattr(component, "engine", None)
    if engine:
        return str(engine)
    return stubs.STUB if type(component).__module__ == stubs.__name__ else type(component).__name__


@dataclass
class Worker:
    session: Session
    pipeline: Pipeline
    raw: RawStore
    clock: Clock
    tenant_id: UUID | None = None  # None: every tenant's jobs
    max_attempts: int = queue.MAX_ATTEMPTS
    backoff: dt.timedelta = BACKOFF

    # --- Loop --------------------------------------------------------------------------------

    def run_one(self) -> bool:
        """Claim and run one job. False when nothing is due."""
        s = self.session
        job = queue.claim(s, self.tenant_id)
        if job is None:
            s.commit()
            return False
        job_id = job.id
        s.commit()
        try:
            result = self._run(job)
            job = s.get(Job, job_id)
            job.raw_model_output = result.output
            job.processed_by = result.processed_by
            job.status = queue.DONE
            job.last_error = None
            job.run_after = None
            job.updated_at = func.now()
            if result.next_stage is not None:
                doc_key = self._doc(job).natural_key
                queue.enqueue(s, job.tenant_id, job.document_id, doc_key, result.next_stage)
            s.commit()
        except Exception as e:
            s.rollback()
            log.warning("job %s failed: %r", job_id, e)
            self._failed(job_id, e)
            s.commit()
        except BaseException:
            self._release(job_id)
            raise
        return True

    def drain(self) -> int:
        """Run jobs until none is due. Returns how many ran."""
        self._recover()
        n = 0
        while self.run_one():
            n += 1
        return n

    def serve(self, poll_seconds: float = 1.0) -> None:
        """Poll forever. Stale jobs are recovered at startup and after every idle poll, so a
        job a stopped worker left `running` comes back without a restart."""
        self._recover()
        while True:
            if not self.run_one():
                time.sleep(poll_seconds)
                self._recover()

    def _recover(self) -> None:
        n = queue.recover_stale(
            self.session, tenant_id=self.tenant_id, max_attempts=self.max_attempts
        )
        self.session.commit()
        if n:
            log.info("recovered %d stale running jobs", n)

    def _release(self, job_id: UUID) -> None:
        """Interrupted (Ctrl-C, SIGTERM): hand the claim back without using up an attempt.
        Best effort: if this fails too, `recover_stale` picks the job up later."""
        s = self.session
        try:
            s.rollback()
            job = s.get(Job, job_id)
            job.status = queue.PENDING
            job.attempts -= 1
            job.claimed_at = None
            job.updated_at = func.now()
            s.commit()
        except Exception:
            log.exception("could not release job %s", job_id)

    def _failed(self, job_id: UUID, e: Exception) -> None:
        job = self.session.get(Job, job_id)
        job.last_error = f"{type(e).__name__}: {e}"[:4000]
        job.updated_at = func.now()
        if isinstance(e, NON_RETRYABLE):
            job.status = queue.FAILED
        elif job.attempts < self.max_attempts:
            job.status = queue.PENDING
            job.run_after = func.now() + self.backoff * 2 ** (job.attempts - 1)
        else:
            job.status = queue.PARKED

    # --- Stages ------------------------------------------------------------------------------

    def _run(self, job: Job) -> StageResult:
        if job.stage == queue.RECONCILE:
            return self._reconcile(job)
        row = self._doc(job)
        if job.stage == queue.CLASSIFY:
            return self._classify(self._text(row, None))
        classification = Classification.model_validate(self._output(job, row, queue.CLASSIFY))
        doc = self._text(row, classification.doc_class)
        if job.stage == queue.EXTRACT:
            return self._extract(doc)
        if job.stage == queue.VALIDATE:
            extraction = Extraction.model_validate(self._output(job, row, queue.EXTRACT))
            return self._validate(doc, extraction)
        raise Permanent(f"unknown stage {job.stage!r}")

    def _classify(self, doc: DocumentText) -> StageResult:
        c = self.pipeline.classifier.classify(doc)
        nxt = None if c.doc_class == DocumentClass.noise else NEXT_STAGE[queue.CLASSIFY]
        return StageResult(c.model_dump(mode="json"), c.engine, nxt)

    def _extract(self, doc: DocumentText) -> StageResult:
        for extractor in self.pipeline.extractors:
            if (x := extractor.extract(doc)) is not None:
                return StageResult(x.model_dump(mode="json"), x.engine, NEXT_STAGE[queue.EXTRACT])
        raise Permanent("no extractor matched")

    def _validate(self, doc: DocumentText, extraction: Extraction) -> StageResult:
        """Vendor resolution in the order `leo.interfaces` sets: look up (never create), validate
        against that vendor's prior terms, and create the vendor only if `vendor_ok`."""
        p = self.pipeline
        name = extraction.vendor_name
        found = p.vendors.find_vendor(name) if name is not None else None
        prior = p.prior_terms(found) if found is not None else []
        v = p.validator.validate(doc, extraction, prior)
        if v.vendor_ok:
            out = v.extraction.with_vendor(p.vendors.vendor_for_name(v.extraction.vendor_name))
        else:  # fail closed: no vendor, every item for review
            x = v.extraction.with_vendor("")
            review = {"status": TermStatus.needs_review}
            out = x.model_copy(
                update={
                    "terms": [t.model_copy(update=review) for t in x.terms],
                    "invoice_facts": [f.model_copy(update=review) for f in x.invoice_facts],
                }
            )
        v = v.model_copy(update={"extraction": out})
        return StageResult(v.model_dump(mode="json"), v.engine, NEXT_STAGE[queue.VALIDATE])

    def _reconcile(self, job: Job) -> StageResult:
        """The whole tenant's ledger, then a draft per finding."""
        ledger = load_ledger(self.session, job.tenant_id)
        today = self.clock.today()
        rec = self.pipeline.reconciler.reconcile(ledger, today)
        drafts = [self.pipeline.drafter.draft(f, ledger) for f in rec.findings]
        output = {
            "today": today.isoformat(),
            "reconciliation": rec.model_dump(mode="json"),
            "drafts": [d.model_dump(mode="json") for d in drafts],
        }
        return StageResult(output, _engine(self.pipeline.reconciler), None)

    # --- Reads -------------------------------------------------------------------------------

    def _doc(self, job: Job) -> DocumentRow:
        row = self.session.scalars(
            select(DocumentRow).where(
                DocumentRow.tenant_id == job.tenant_id, DocumentRow.id == job.document_id
            )
        ).one()
        return row

    def _text(self, row: DocumentRow, doc_class: DocumentClass | None) -> DocumentText:
        """`doc_class` comes from the classify job: A4 doesn't write it to the row."""
        document = SourceDocument(
            path=row.path,
            doc_class=doc_class,
            vendor=None if row.vendor_id is None else str(row.vendor_id),
            sha256=row.content_sha256,
            notes=row.notes,
        )
        return DocumentText(
            document=document, text=read_source_text(self.raw.path(row.storage_path))
        )

    def _output(self, job: Job, row: DocumentRow, stage: str) -> dict[str, Any]:
        """A finished earlier stage's output for the same document."""
        out = self.session.scalar(
            select(Job.raw_model_output).where(
                Job.tenant_id == job.tenant_id,
                Job.natural_key == keys.job(row.natural_key, stage),
                Job.status == queue.DONE,
            )
        )
        if out is None:
            raise Permanent(f"no finished {stage} job for {row.path}")
        return out
