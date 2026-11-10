"""PostgreSQL adapter to the existing A1 Job model. No schema migration needed."""
from __future__ import annotations

import os
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from leo.db.models import Job
from leo.db.session import get_engine
from leo.worker.core import NEXT_STAGE, StageOutcome, WorkItem


class PostgresJobRepository:
    def __init__(self, sessions: sessionmaker[Session] | None = None, *, worker_id: str | None = None):
        self.sessions = sessions or sessionmaker(bind=get_engine(), expire_on_commit=False)
        self.worker_id = worker_id or f"single-worker:{os.getpid()}"

    def claim_next(self) -> WorkItem | None:
        with self.sessions.begin() as db:
            job = db.scalar(
                select(Job)
                .where(Job.status == "pending")
                .order_by(Job.created_at.asc(), Job.id.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            job.status = "running"
            job.processed_by = self.worker_id
            return WorkItem(
                id=job.id, document_id=job.document_id, tenant_id=job.tenant_id,
                stage=job.stage, attempts=job.attempts,
                checkpoints=dict(job.raw_model_output or {}),
            )

    def record_attempt(self, job_id: UUID, stage: str) -> int:
        with self.sessions.begin() as db:
            job = db.get(Job, job_id, with_for_update=True)
            if job is None or job.status != "running" or job.stage != stage:
                raise RuntimeError("Job is not running at expected stage")
            job.attempts += 1
            return job.attempts

    def advance(self, job_id: UUID, stage: str, outcome: StageOutcome) -> None:
        with self.sessions.begin() as db:
            job = db.get(Job, job_id, with_for_update=True)
            if job is None or job.status != "running" or job.stage != stage:
                raise RuntimeError("Job is not running at expected stage")
            # Reassign JSONB column rather than mutating an untracked dict in place.
            job.raw_model_output = {**(job.raw_model_output or {}), stage: outcome.checkpoint}
            next_stage = NEXT_STAGE[stage]
            job.stage = next_stage or stage  # schema stage is NOT NULL
            job.status = "completed" if outcome.stop or next_stage is None else "running"
            job.attempts = 0
            job.last_error = None

    def fail(self, job_id: UUID, error: str, *, review: bool = False) -> None:
        with self.sessions.begin() as db:
            job = db.get(Job, job_id, with_for_update=True)
            if job is None:
                raise RuntimeError("Job not found")
            job.status = "needs_review" if review else "failed"
            job.last_error = error[:4000]
