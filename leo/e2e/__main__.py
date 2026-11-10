"""`python -m leo.e2e` (`make e2e`): reset the dev database, run the pipeline on the synthetic
set, and check the result against the answer key (ROADMAP 2.2). Run it before every merge.

1. `make reset`: drop, migrate, seed. This wipes every local approval and Gmail row.
2. Ingest `data/synthetic` into the pipeline's tenant (`LEO_PIPELINE_TENANT`) and drain it,
   committed, so `make api-pipeline` shows the result afterwards.
3. Check F1-F5, no extra finding, N1-N8 and D1-D4, then ingest and reconcile again (D5).

Live by default: the Jev classifier (`JEV_API_KEY`), scripts then the cached LLM extractor,
the rules validator and the cached LLM drafter. `--offline` uses the key's classes and sets
`LEO_LLM_OFFLINE=1`, so a cache miss fails instead of calling the API. The clock is pinned to
the key's today. Exits 1 if any check fails.
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
        )
    except JevUnavailable as e:
        parser.error(f"{e} (or pass --offline)")
    mode = "offline: key classes, cached LLM only" if args.offline else "live: Jev classifier"
    print(f"make e2e ({mode}), today {fx.demo_today}, tenant {slug}", flush=True)

    # No session may be open here: it would block the reset's DROP SCHEMA.
    with _step("reset"):
        reset.main([])

    store = RawStore(settings.leo_raw_dir)
    with get_sessionmaker()() as session:
        with _step("ingest"):
            tenant_id, new = folder.load(session, DATA, slug, store)
            session.commit()
            print(f"   {new} documents")
        worker = Worker(session, pipeline, store, clock, tenant_id=tenant_id)
        with _step("pipeline"):
            print(f"   ran {worker.drain()} jobs")

        with _step("checks"):
            checks = run_checks(session, tenant_id, fx)

            def rerun() -> int:
                _, again = folder.load(session, DATA, slug, store)
                queue.request_reconcile(session, tenant_id)
                session.commit()
                worker.drain()
                return again

            checks.append(check_rerun(session, tenant_id, rerun))

        engines = draft_engines(session, tenant_id)

    print()
    for c in checks:
        print(f"{'PASS' if c.ok else 'FAIL'}  {c.id:<6} {c.detail}")
    print("\nDrafts:")
    for title, engine in engines.items():
        print(f"  {engine:<28} {title}")
    failed = [c.id for c in checks if not c.ok]
    total = time.monotonic() - started
    passed = len(checks) - len(failed)
    print(f"\n{passed}/{len(checks)} checks passed in {total:.0f}s", end="")
    print(f"; failed: {', '.join(failed)}" if failed else "")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
