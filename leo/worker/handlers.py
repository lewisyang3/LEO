"""Integration seam: wire existing B2/B3/B4, A2, C1 and draft implementations here.

Keep SDK retries OFF in the B-stage clients. Raise RetryableStageError for
429/5xx/timeouts (use Retry-After where available), and ReviewRequired for
unusable extracted data. Stage handlers should be idempotent on rerun.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from leo.worker.core import StageOutcome, WorkItem


def build_handlers(
    *,
    classify: Callable[[WorkItem], StageOutcome],
    extract: Callable[[WorkItem], StageOutcome],
    validate: Callable[[WorkItem], StageOutcome],
    persist: Callable[[WorkItem], StageOutcome],
    reconcile: Callable[[WorkItem], StageOutcome],
    draft: Callable[[WorkItem], StageOutcome],
) -> dict[str, Callable[[WorkItem], StageOutcome]]:
    """Register existing pipeline functions without assuming their signatures."""
    return dict(classify=classify, extract=extract, validate=validate,
                persist=persist, reconcile=reconcile, draft=draft)
