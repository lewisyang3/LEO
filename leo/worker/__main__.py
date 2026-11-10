"""`python -m leo.worker`: run the worker against `DATABASE_URL`, with the stub pipeline.

`--drain` runs until no job is due, then exits. Without it the worker polls forever.
"""

import argparse
import logging
import signal
import sys

from leo.clock import get_clock
from leo.config import get_settings
from leo.db.session import get_sessionmaker
from leo.ingest.store import RawStore
from leo.worker.pipeline import Pipeline
from leo.worker.run import Worker


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m leo.worker", description=__doc__)
    parser.add_argument("--drain", action="store_true", help="exit when no job is due")
    parser.add_argument("--poll", type=float, default=1.0, help="seconds between polls")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # SIGTERM (docker stop, a supervisor) exits through SystemExit, so the worker hands its
    # claimed job back instead of leaving it `running`.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    settings = get_settings()
    with get_sessionmaker()() as session:
        worker = Worker(session, Pipeline.stubs(), RawStore(settings.leo_raw_dir), get_clock())
        if args.drain:
            logging.info("ran %d jobs", worker.drain())
        else:
            worker.serve(args.poll)


if __name__ == "__main__":
    main()
