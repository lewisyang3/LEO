"""One-email-at-a-time workflow runner for Leo.

Handlers own business operations; the worker owns stage order, checkpointing,
errors and bounded retries. All returned checkpoints must be JSON-serializable.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID

LOG = logging.getLogger(__name__)
STAGES = ("classify", "extract", "validate", "persist", "reconcile", "draft")
NEXT_STAGE = dict(zip(STAGES, (*STAGES[1:], None)))


class RetryableStageError(Exception):
    """Temporary external failure. retry_after is in seconds, when provided."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ReviewRequired(Exception):
    """Data needs review; not a transient API failure."""


@dataclass(frozen=True)
class WorkItem:
    id: UUID
    document_id: UUID
    tenant_id: UUID
    stage: str
    attempts: int = 0  # attempts within current stage, including initial attempt
    checkpoints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageOutcome:
    """Only small, safe metadata in checkpoint; save business rows in handlers."""
    checkpoint: dict[str, Any] = field(default_factory=dict)
    stop: bool = False  # e.g. noise email, no actionable findings


class JobRepository(Protocol):
    def claim_next(self) -> WorkItem | None: ...
    def record_attempt(self, job_id: UUID, stage: str) -> int: ...
    def advance(self, job_id: UUID, stage: str, outcome: StageOutcome) -> None: ...
    def fail(self, job_id: UUID, error: str, *, review: bool = False) -> None: ...


StageHandler = Callable[[WorkItem], StageOutcome]


class SingleWorker:
    def __init__(
        self,
        repo: JobRepository,
        handlers: Mapping[str, StageHandler],
        *,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if set(handlers) != set(STAGES):
            raise ValueError(f"Handlers required for exactly these stages: {STAGES}")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.repo = repo
        self.handlers = handlers
        self.max_retries = max_retries
        self.sleep = sleep
        self.jitter = jitter

    def run_one(self) -> bool:
        """Consume at most one queued email; True if one was claimed."""
        job = self.repo.claim_next()
        if job is None:
            return False
        if job.stage not in STAGES:
            self.repo.fail(job.id, f"Unknown stage: {job.stage}")
            return True

        stage = job.stage
        attempts = job.attempts
        checkpoints = dict(job.checkpoints)

        while stage is not None:
            # Previous attempts count toward the 1 + max_retries budget.
            if attempts >= 1 + self.max_retries:
                self.repo.fail(job.id, f"Retry budget exhausted at {stage}")
                return True
            attempts = self.repo.record_attempt(job.id, stage)
            current = WorkItem(job.id, job.document_id, job.tenant_id, stage, attempts, dict(checkpoints))
            try:
                outcome = self.handlers[stage](current)
                if not isinstance(outcome, StageOutcome):
                    raise TypeError(f"{stage} handler must return StageOutcome")
                self.repo.advance(job.id, stage, outcome)
                checkpoints[stage] = outcome.checkpoint
                if outcome.stop:
                    return True
                stage = NEXT_STAGE[stage]
                attempts = 0
            except RetryableStageError as exc:
                if attempts >= 1 + self.max_retries:
                    self.repo.fail(job.id, f"{stage}: {exc} (retry limit reached)")
                    return True
                delay = exc.retry_after if exc.retry_after is not None else min(60.0, 2 ** (attempts - 1) + self.jitter())
                self.sleep(max(0.0, delay))
            except ReviewRequired as exc:
                self.repo.fail(job.id, f"{stage}: {exc}", review=True)
                return True
            except Exception as exc:
                LOG.exception("Job %s failed at stage %s", job.id, stage)
                self.repo.fail(job.id, f"{stage}: {type(exc).__name__}: {exc}")
                return True
        return True

    def serve_forever(self, *, poll_seconds: float = 1.0) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be > 0")
        while True:
            if not self.run_one():
                self.sleep(poll_seconds)
