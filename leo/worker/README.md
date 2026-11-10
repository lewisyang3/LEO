# Leo A4 — single persistent FIFO worker (starter)

## Included

- `leo/worker/core.py`: one-email-at-a-time orchestrator; stages `classify → extract → validate → persist → reconcile → draft`; 3 *additional* transient retries (4 total tries per stage); backoff, Retry-After; permanent failures / review; FIFO job completion.
- `leo/worker/postgres.py`: SQLAlchemy adapter for the existing A1 `Job` table. Atomic FIFO claim, persisted attempts, JSONB checkpoint plus stage transition in one transaction.
- `leo/worker/handlers.py`: explicit hookup for your existing B2/B3/B4, persistence, reconciliation, draft functions. **Not implemented in this package** because their actual signatures/code are unavailable.
- `tests/test_a4_worker.py`: isolated state-machine tests (fake repository, no network/DB).

## Integrate

Copy `leo/worker/` into the repo and the test into `tests/`. Then:

```bash
uv run pytest tests/test_a4_worker.py -q
```

Bind actual functions using `build_handlers()` in your app composition root, instantiate `PostgresJobRepository()` and `SingleWorker(repo, handlers)`, and call `worker.serve_forever()` in a dedicated Python process. A3 must create **one** `Job` row per ingested email with `stage='classify'`, `status='pending'`, `attempts=0`, `raw_model_output={}` (plus document_id, tenant_id, unique natural_key); no schema migration necessary if A1 matches these fields.

Example **integration skeleton**, not runnable until the handlers are wired:

```python
from leo.worker.core import SingleWorker
from leo.worker.postgres import PostgresJobRepository
from leo.worker.handlers import build_handlers

handlers = build_handlers(
    classify=my_classify_handler,
    extract=my_extract_handler,
    validate=my_validate_handler,
    persist=my_upsert_handler,
    reconcile=my_reconcile_handler,
    draft=my_draft_handler,
)
SingleWorker(PostgresJobRepository(), handlers).serve_forever()
```

Handlers receive `WorkItem` with `document_id`, `tenant_id`, `stage`, `checkpoints`; return `StageOutcome(checkpoint={...})`. `StageOutcome(stop=True)` finishes workflow early (noise, no findings). Save draft from draft handler and return `StageOutcome` only after it has been stored; worker then marks job complete. **No approval or send step**.

### Important integration / production follow-ups

1. Turn **off SDK automatic retries** for B2/B3 to avoid retry multiplication. Raise `RetryableStageError` only for timeouts, 429 or transient 5xx; pass `retry_after` if applicable. Rate-limit proactive requests as well.
2. Handlers that write business tables must use A2 upserts and be idempotent. **The worker cannot make a handler's business-row write atomic with the job checkpoint**; for strict exactly-once effects, use a shared transaction or idempotency keys. The current single worker achieves *at-least-once* handler execution on crashes.
3. The existing A1 `Job` schema lacks `next_retry_at`, `lease_expires_at`, `completed_at`. Retries sleep in the worker process; a crash while `running` requires manual recovery. Add lease/recovery logic before production deployment.
4. `raw_model_output` is used here for **small JSON-safe stage checkpoints**. Avoid raw email content, secrets, or unbounded model responses in this field. Full extracted records belong in domain tables.
5. FIFO claim is determined by `job.created_at, job.id`; jobs are not preempted while retrying. Newer emails continue when older emails finish or fail. The `FOR UPDATE SKIP LOCKED` clause provides safety even with a mistaken second process, though the designed deployment runs only one worker.

## Launch command

Once you have a Python function `myapp.worker_wiring.make_handlers()` returning the six handlers, run:

```bash
LEO_WORKER_HANDLERS=myapp.worker_wiring:make_handlers uv run python -m leo.worker
```

For a Makefile target (tabs required):

```makefile
worker:
	uv run python -m leo.worker
```

Set `LEO_WORKER_HANDLERS` in your environment or `.env` loading mechanism. The worker will refuse to start unless you've supplied real handlers; no fake AI outputs or emails will be generated silently.
