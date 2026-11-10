"""The job queue (ROADMAP A4): rows in `job`, claimed with `FOR UPDATE SKIP LOCKED`.

There is one row per (document, stage), keyed `keys.job(doc_key, stage)`. A job is `pending`
until a worker claims it (`running`). It ends `done`, `failed` (an error retrying can't fix) or
`parked` (out of retries, waiting for a person). A retried job goes back to `pending` with
`run_after` set, so the worker serves other jobs while it waits.

None of these functions commit: the caller owns the transaction.
"""

import datetime as dt
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leo.db import keys
from leo.db.models import Job, new_uuid7

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
PARKED = "parked"

CLASSIFY = "classify"
EXTRACT = "extract"
VALIDATE = "validate"
RECONCILE = "reconcile"  # reconcile the tenant's ledger, then draft each finding
STAGES = (CLASSIFY, EXTRACT, VALIDATE, RECONCILE)

STALE_AFTER = dt.timedelta(minutes=10)
MAX_ATTEMPTS = 3
STOPPED = "worker stopped during the stage, out of attempts"


def enqueue(session: Session, tenant_id: UUID, document_id: UUID, doc_key: str, stage: str) -> None:
    """Queue `stage` for a document. A job that already exists, in any status, is left alone,
    so re-ingesting a document never requeues it. (`Repo.upsert_job` always updates.)"""
    session.execute(
        insert(Job)
        .values(
            id=new_uuid7(),
            tenant_id=tenant_id,
            natural_key=keys.job(doc_key, stage),
            document_id=document_id,
            stage=stage,
            status=PENDING,
            attempts=0,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "natural_key"])
    )


def claim(session: Session, tenant_id: UUID | None = None) -> Job | None:
    """Take the oldest due `pending` job, skipping rows another worker has locked. Marks it
    `running` and counts the attempt now, so a crash mid-stage still uses one up. The caller
    commits to release the lock."""
    q = (
        select(Job)
        .where(Job.status == PENDING, or_(Job.run_after.is_(None), Job.run_after <= func.now()))
        .order_by(Job.created_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if tenant_id is not None:
        q = q.where(Job.tenant_id == tenant_id)
    job = session.scalars(q).first()
    if job is None:
        return None
    job.status = RUNNING
    job.claimed_at = func.now()
    job.attempts += 1
    job.updated_at = func.now()
    session.flush()
    session.refresh(job)
    return job


def recover_stale(
    session: Session,
    older_than: dt.timedelta = STALE_AFTER,
    tenant_id: UUID | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> int:
    """`running` jobs claimed longer ago than `older_than` lost their worker. Those out of
    attempts are `parked` (a stage that kills the worker every time stops being retried); the
    rest go back to `pending`. Returns how many were changed."""
    stale = [Job.status == RUNNING, Job.claimed_at < func.now() - older_than]
    if tenant_id is not None:
        stale.append(Job.tenant_id == tenant_id)
    parked = session.execute(
        update(Job)
        .where(*stale, Job.attempts >= max_attempts)
        .values(status=PARKED, last_error=STOPPED, updated_at=func.now())
    ).rowcount
    reset = session.execute(
        update(Job).where(*stale).values(status=PENDING, updated_at=func.now())
    ).rowcount
    return parked + reset
