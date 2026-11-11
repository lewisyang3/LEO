"""`python -m leo.e2e` (`make e2e`): reset the dev database, run the pipeline on the synthetic
set, and check the result against the answer key. Run it before every merge.

1. `make reset`: drop, migrate, seed. This wipes every local approval and Gmail row.
2. Ingest `data/synthetic` into the pipeline's tenant (`LEO_PIPELINE_TENANT`) and drain it,
   committed, so `make api-pipeline` shows the result afterwards.
3. Check F1-F5 (each with its draft), no extra finding, the vendor names, N1-N8, D1-D4 and
   that no draft fell back to the template (2.3), then ingest and reconcile again (D5).

Live by default: the Jev classifier (`JEV_API_KEY`), scripts then the cached LLM extractor,
the rules validator and the cached LLM drafter. `--offline` uses the key's classes and sets
`LEO_LLM_OFFLINE=1`, so a cache miss fails instead of calling the API. The clock is pinned to
the key's today. Exits 1 if any check fails.

Script generation (Phase 4): `--no-builtin-scripts` leaves out the hand-written scripts, so
their formats go to the LLM. `--keep-scripts` carries the pipeline tenant's promoted generated
scripts (`make scriptgen`) across the reset, so the run extracts with them.
"""

import argparse
import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DATA = Path("data/synthetic")


@contextmanager
def _step(name: str) -> Iterator[None]:
    print(f"== {name}", flush=True)
    start = time.monotonic()
    yield
    print(f"   {name}: {time.monotonic() - start:.1f}s", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leo.e2e", description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="the key's classes and cached LLM answers only (no network)",
    )
    parser.add_argument(
        "--no-builtin-scripts",
        action="store_true",
        help="leave out the hand-written scripts (B5)",
    )
    parser.add_argument(
        "--keep-scripts",
        action="store_true",
        help="keep the promoted generated scripts across the reset (make scriptgen)",
    )
    args = parser.parse_args(argv)

    # Before anything reads the settings: they are cached, and the reset reads them.
    if args.offline:
        os.environ["LEO_LLM_OFFLINE"] = "1"
    from leo.config import get_settings

    get_settings.cache_clear()

    from leo.clock import FixedClock
    from leo.db import reset
    from leo.db.session import get_sessionmaker
    from leo.e2e.checks import check_rerun, draft_engines, run_checks
    from leo.fixtures import load_fixture
    from leo.ingest import folder
    from leo.ingest.store import RawStore
    from leo.jev import JevUnavailable
    from leo.worker import queue
    from leo.worker.pipeline import Pipeline
    from leo.worker.run import Worker

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    fx = load_fixture()
    clock = FixedClock(fx.demo_today)
    slug = settings.leo_pipeline_tenant
    started = time.monotonic()

    # Build the pipeline first, so a missing JEV_API_KEY stops the run before the reset.
    try:
        pipeline = Pipeline.default(
            clock,
            classifier="fixture" if args.offline else "jev",
            validator="rules",
            drafter="llm",
            sender=fx.customer.name,
            settings=settings,
            builtin_scripts=not args.no_builtin_scripts,
        )
    except JevUnavailable as e:
        parser.error(f"{e} (or pass --offline)")
    mode = "offline: key classes, cached LLM only" if args.offline else "live: Jev classifier"
    if args.no_builtin_scripts:
        mode += ", no hand-written scripts"
    print(f"make e2e ({mode}), today {fx.demo_today}, tenant {slug}", flush=True)

    kept = []
    if args.keep_scripts:
        with get_sessionmaker()() as session:
            kept = saved_scripts(session, slug)
        print(f"   keeping {len(kept)} promoted generated script(s)", flush=True)

    # No session may be open here: it would block the reset's DROP SCHEMA.
    with _step("reset"):
        reset.main([])

    store = RawStore(settings.leo_raw_dir)
    with get_sessionmaker()() as session:
        with _step("ingest"):
            tenant_id, new = folder.load(session, DATA, slug, store)
            restore_scripts(session, tenant_id, kept)
            session.commit()
            print(f"   {new} documents")
        worker = Worker(session, pipeline, store, clock, tenant_id=tenant_id)
        with _step("pipeline"):
            print(f"   ran {worker.drain()} jobs")

        with _step("checks"):
            checks = run_checks(session, tenant_id, fx, llm_drafts=True)

            def rerun() -> int:
                _, again = folder.load(session, DATA, slug, store)
                queue.request_reconcile(session, tenant_id)
                session.commit()
                worker.drain()
                return again

            checks.append(check_rerun(session, tenant_id, rerun))

        engines = draft_engines(session, tenant_id)
        extract = extract_engines(session, tenant_id)

    print()
    for c in checks:
        print(f"{'PASS' if c.ok else 'FAIL'}  {c.id:<6} {c.detail}")
    print("\nExtraction:")
    for engine, n in extract.items():
        print(f"  {n:>3}  {engine}")
    print("\nDrafts:")
    for title, engine in engines.items():
        print(f"  {engine:<28} {title}")
    failed = [c.id for c in checks if not c.ok]
    total = time.monotonic() - started
    passed = len(checks) - len(failed)
    print(f"\n{passed}/{len(checks)} checks passed in {total:.0f}s", end="")
    print(f"; failed: {', '.join(failed)}" if failed else "")
    return 1 if failed else 0


def saved_scripts(session, slug: str) -> list[dict]:
    """The tenant's promoted generated scripts, as plain values that outlive the reset."""
    from leo.db.models import Tenant
    from leo.db.repo import Repo

    tenant_id = session.query(Tenant.id).filter(Tenant.slug == slug).scalar()
    if tenant_id is None:
        return []
    return [
        {"name": r.name, "code": r.code, "matcher": r.matcher, "accuracy": r.accuracy}
        for r in Repo(session, tenant_id).promoted_scripts()
    ]


def restore_scripts(session, tenant_id, scripts: list[dict]) -> None:
    import datetime as dt

    from leo.db.repo import Repo

    repo = Repo(session, tenant_id)
    for s in scripts:
        repo.add_extractor_script(**s, promoted_at=dt.datetime.now(dt.UTC))


def extract_engines(session, tenant_id) -> dict[str, int]:
    """How many documents each extract engine took, most first."""
    from sqlalchemy import func, select

    from leo.db.models import Job
    from leo.worker import queue

    rows = session.execute(
        select(Job.processed_by, func.count())
        .where(Job.tenant_id == tenant_id, Job.stage == queue.EXTRACT, Job.status == queue.DONE)
        .group_by(Job.processed_by)
        .order_by(func.count().desc(), Job.processed_by)
    ).all()
    return {str(engine): n for engine, n in rows}


if __name__ == "__main__":
    sys.exit(main())
