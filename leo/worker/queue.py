"""The job queue (ROADMAP A4): rows in `job`, claimed with `FOR UPDATE SKIP LOCKED`.

There is one row per (document, stage), keyed `keys.job(doc_key, stage)`, except reconcile:
one row per tenant (`keys.tenant_job`), requested again by each validated document and claimed
only once the tenant has nothing else pending or running (2.1). So N documents give one
reconcile round, not N. A job is `pending`
until a worker claims it (`running`). It ends `done`, `failed` (an error retrying can't fix) or
`parked` (out of retries, waiting for a person). A retried job goes back to `pending` with
`run_after` set, so the worker serves other jobs while it waits.

None of these functions commit: the caller owns the transaction.
"""

import datetime as dt
from uuid import UUID

from sqlalchemy import exists, func, or_, select, update
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
RECONCILE = "reconcile"  # per tenant: match, reconcile the ledger, then draft each finding
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
            # The wall clock, not the transaction start: jobs queued in one transaction then
            # run in the order they were queued (file order), every run.
            created_at=func.clock_timestamp(),
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "natural_key"])
    )


def request_reconcile(session: Session, tenant_id: UUID) -> None:
    """Ask for a reconcile round over the tenant. A pending request is left as it is (the
    debounce); a finished, failed or parked one goes back to `pending` with fresh attempts.

    With one worker a request can't arrive while reconcile is running (the worker is busy
    with the job making the request). With several, a request made then is lost until the
    next one (ROADMAP 2.1)."""
    ins = insert(Job).values(
        id=new_uuid7(),
        tenant_id=tenant_id,
        natural_key=keys.tenant_job(str(tenant_id), RECONCILE),
        document_id=None,
        stage=RECONCILE,
        status=PENDING,
        attempts=0,
        created_at=func.clock_timestamp(),
    )
    session.execute(
        ins.on_conflict_do_update(
            index_elements=["tenant_id", "natural_key"],
            set_={
                "status": PENDING,
                "attempts": 0,
                "last_error": None,
                "run_after": None,
                "claimed_at": None,
                "created_at": func.clock_timestamp(),
                "updated_at": func.now(),
            },
            where=Job.status.in_((DONE, FAILED, PARKED)),
        )
    )


def _not_blocked():
    """A reconcile job is claimable only while its tenant has no other job pending (due or
    not) or running; any other stage always is. Shared by `claim` and `seconds_until_due`."""
    other = Job.__table__.alias("other")
    busy = exists().where(
        other.c.tenant_id == Job.tenant_id,
        other.c.stage != RECONCILE,
        other.c.status.in_((PENDING, RUNNING)),
    )
    return or_(Job.stage != RECONCILE, ~busy)


def claim(session: Session, tenant_id: UUID | None = None) -> Job | None:
    """Take the oldest due `pending` job, skipping rows another worker has locked. Marks it
    `running` and counts the attempt now, so a crash mid-stage still uses one up. The caller
    commits to release the lock.

    A reconcile job waits while its tenant has any other job pending (due or not) or running,
    so it reconciles once the documents are through."""
    q = (
        select(Job)
        .where(
            Job.status == PENDING,
            # The wall clock, as `seconds_until_due` uses: a drain that waited out a backoff
            # must then see the job as due, even inside one long transaction.
            or_(Job.run_after.is_(None), Job.run_after <= func.clock_timestamp()),
            _not_blocked(),
        )
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


def seconds_until_due(session: Session, tenant_id: UUID | None = None) -> float | None:
    """Seconds until the earliest `pending` job waiting on `run_after` (a retry's backoff)
    is due, or None when no job is waiting. Uses the wall clock, not the transaction's.

    A reconcile that `claim` would refuse (its tenant has a job running) doesn't count, so a
    drain doesn't spin on it; a pending document job that blocks it does, and is waited for."""
    q = select(func.extract("epoch", func.min(Job.run_after) - func.clock_timestamp())).where(
        Job.status == PENDING, Job.run_after.is_not(None), _not_blocked()
    )
    if tenant_id is not None:
        q = q.where(Job.tenant_id == tenant_id)
    wait = session.scalar(q)
    return None if wait is None else max(0.0, float(wait))


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
