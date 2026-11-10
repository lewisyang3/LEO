"""`python -m leo.worker`: run the worker against `DATABASE_URL` with the real pipeline (2.1).

`--drain` runs until no job is due, then exits. Without it the worker polls forever.
Defaults: the Jev classifier (live, needs `JEV_API_KEY`), scripts then the cached LLM
extractor, the rules validator, and the cached LLM drafter. `--classifier fixture` and
`--drafter template` run with no network once the extract cache covers the documents
(`LEO_LLM_OFFLINE=1` makes a cache miss an error instead of an API call).
"""

import argparse
import logging
import signal
import sys
from pathlib import Path

from leo.clock import get_clock
from leo.config import get_settings
from leo.db.models import Tenant
from leo.db.session import get_sessionmaker
from leo.fixtures import load_fixture
from leo.ingest.store import RawStore
from leo.worker.pipeline import DEFAULT_TRANSACTIONS, Pipeline
from leo.worker.run import Worker


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m leo.worker", description=__doc__)
    parser.add_argument("--drain", action="store_true", help="exit when no job is due")
    parser.add_argument("--poll", type=float, default=1.0, help="seconds between polls")
    parser.add_argument("--classifier", choices=("jev", "fixture"), default="jev")
    parser.add_argument("--validator", choices=("rules", "jev"), default="rules")
    parser.add_argument("--drafter", choices=("llm", "template"), default="llm")
    parser.add_argument(
        "--transactions",
        default=str(DEFAULT_TRANSACTIONS),
        help="card charges synced at each reconcile ('' for none)",
    )
    parser.add_argument("--tenant", help="only this tenant's jobs (slug); default every tenant")
    parser.add_argument("--stubs", action="store_true", help="A4's stub pipeline")
    parser.add_argument(
        "--no-builtin-scripts",
        action="store_true",
        help="leave out the hand-written scripts (B5); promoted generated ones still run",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # SIGTERM (docker stop, a supervisor) exits through SystemExit, so the worker hands its
    # claimed job back instead of leaving it `running`.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    settings = get_settings()
    clock = get_clock()
    if args.stubs:
        pipeline = Pipeline.stubs()
    else:
        pipeline = Pipeline.default(
            clock,
            classifier=args.classifier,
            validator=args.validator,
            drafter=args.drafter,
            transactions=Path(args.transactions) if args.transactions else None,
            sender=load_fixture().customer.name,
            settings=settings,
            builtin_scripts=not args.no_builtin_scripts,
        )
    with get_sessionmaker()() as session:
        tenant_id = None
        if args.tenant:
            tenant_id = session.query(Tenant.id).filter(Tenant.slug == args.tenant).scalar()
            if tenant_id is None:
                parser.error(f"no tenant {args.tenant!r} (run make ingest)")
        worker = Worker(
            session, pipeline, RawStore(settings.leo_raw_dir), clock, tenant_id=tenant_id
        )
        if args.drain:
            logging.info("ran %d jobs", worker.drain())
        else:
            worker.serve(args.poll)


if __name__ == "__main__":
    main()
