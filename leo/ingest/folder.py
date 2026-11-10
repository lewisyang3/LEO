"""`python -m leo.ingest.folder`: ingest a dataset's documents and queue them (ROADMAP 2.1).

`ingest_dataset` (A3) loads ROOT's `inbox/` then `contracts/`, in name order, with paths
relative to ROOT, and `ingest()` queues a `classify` job for each document. The order is the
same every run, so the jobs run in the same order and the first document of each vendor names
it. The tenant defaults to `LEO_PIPELINE_TENANT`, never the seed's, so the pipeline's rows
don't mix with the answer key's. Re-running ingests nothing new.

Then `make worker ARGS=--drain` runs the pipeline.
"""

import argparse
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from leo.config import get_settings
from leo.db.repo import Repo, upsert_tenant
from leo.ingest.ingest import ingest_dataset
from leo.ingest.store import RawStore

DEFAULT_ROOT = Path("data/synthetic")


def load(session: Session, root: Path, slug: str, store: RawStore) -> tuple[UUID, int]:
    """Ingest and queue the dataset under tenant `slug`. Returns (tenant id, new documents).
    Doesn't commit."""
    tenant_id = upsert_tenant(session, slug, slug)
    results = ingest_dataset(Repo(session, tenant_id), store, root)
    return tenant_id, sum(r.created for r in results)


def main(argv: list[str] | None = None) -> None:
    from leo.db.session import get_sessionmaker

    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m leo.ingest.folder", description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--tenant", default=settings.leo_pipeline_tenant)
    args = parser.parse_args(argv)
    with get_sessionmaker()() as session:
        tenant_id, new = load(session, args.root, args.tenant, RawStore(settings.leo_raw_dir))
        session.commit()
    print(f"ingested {new} new documents into {args.tenant} ({tenant_id})")


if __name__ == "__main__":
    main()
