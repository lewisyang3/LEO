"""`python -m leo.scriptgen` (`make scriptgen`): write scripts for formats the LLM keeps extracting.

Reads the pipeline tenant's finished jobs (`LEO_PIPELINE_TENANT`; run `make e2e` first, with
`ARGS=--no-builtin-scripts` to send the hand-written scripts' formats to the LLM). For each
sender with at least `--threshold` verified extractions, it asks the model for a script,
tests it on those documents and on every other sender's, and stores it in `extractor_script`,
promoted only when it reproduces every example and matches nothing else. Up to
`--max-attempts` per sender; each retry sees the previous failures.

Promoted scripts run in the worker before the LLM. `make e2e ARGS="--no-builtin-scripts
--keep-scripts"` carries them across the reset and shows which documents they took.

A reviewer's correction on a generated script's output demotes it: its sender becomes a
candidate again, and the new version must reproduce the corrected results.

`--offline` (or `LEO_LLM_OFFLINE=1`) uses only cached answers (`data/llm_cache/scriptgen`).
`--dry-run` lists the candidate senders and stops.
"""

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leo.scriptgen", description=__doc__)
    parser.add_argument("--tenant", help="tenant slug (default LEO_PIPELINE_TENANT)")
    parser.add_argument("--threshold", type=int, default=None, help="examples per sender (2)")
    parser.add_argument("--max-attempts", type=int, default=None, help="proposals per sender (3)")
    parser.add_argument("--offline", action="store_true", help="cached model answers only")
    parser.add_argument("--dry-run", action="store_true", help="list candidates; generate nothing")
    args = parser.parse_args(argv)

    if args.offline:
        os.environ["LEO_LLM_OFFLINE"] = "1"
    from leo.config import get_settings

    get_settings.cache_clear()

    from sqlalchemy import select

    from leo.clock import FixedClock, get_clock
    from leo.db.models import ExtractorScript, Tenant
    from leo.db.repo import Repo
    from leo.db.session import get_sessionmaker
    from leo.fixtures import load_fixture
    from leo.ingest.store import RawStore
    from leo.llm import ResponseCache
    from leo.scriptgen import generate
    from leo.scriptgen.candidates import DEFAULT_THRESHOLD, find_groups, load_documents, negatives
    from leo.scriptgen.promote import generate_and_promote
    from leo.scriptgen.sandbox import run_isolated

    settings = get_settings()
    slug = args.tenant or settings.leo_pipeline_tenant
    # The pipeline tenant is the synthetic set, whose derived dates use the key's today.
    clock = (
        FixedClock(load_fixture().demo_today)
        if slug == settings.leo_pipeline_tenant and settings.leo_today is None
        else get_clock(settings)
    )
    threshold = args.threshold or DEFAULT_THRESHOLD
    max_attempts = args.max_attempts or generate.MAX_ATTEMPTS
    cache = ResponseCache(settings.leo_llm_cache_dir)

    with get_sessionmaker()() as session:
        tenant_id = session.query(Tenant.id).filter(Tenant.slug == slug).scalar()
        if tenant_id is None:
            parser.error(f"no tenant {slug!r} (run make e2e)")
        repo = Repo(session, tenant_id)
        docs = load_documents(session, tenant_id, RawStore(settings.leo_raw_dir), clock)
        covered = {r.matcher for r in repo.promoted_scripts() if r.matcher}
        groups = find_groups(docs, threshold, covered=covered)
        n_llm = sum(1 for d in docs if (d.extract_engine or "").startswith("llm:"))
        print(
            f"scriptgen: tenant {slug}, {len(docs)} documents, {n_llm} extracted by the LLM, "
            f"model {settings.leo_model_scriptgen}"
        )
        for r in session.scalars(
            select(ExtractorScript)
            .where(ExtractorScript.tenant_id == tenant_id, ExtractorScript.demoted_at.is_not(None))
            .order_by(ExtractorScript.name, ExtractorScript.version)
        ):
            print(
                f"demoted: {r.name}@{r.version} ({r.matcher}), accuracy {float(r.accuracy):.0%} "
                "after reviewers' corrections"
            )
        for d in docs:
            if d.note:
                print(f"   not an example: {d.path}: {d.note}")
        if not groups:
            print(f"No sender has {threshold}+ verified extractions without a script.")
        for g in groups:
            print(f"\n== {g.sender}: {len(g.examples)} examples")
            for e in g.examples:
                print(f"   {e.path}")
        if args.dry_run or not groups:
            return 0

        promoted = 0
        for g in groups:
            out = generate_and_promote(
                repo,
                g,
                negatives(docs, g),
                clock,
                cache,
                settings=settings,
                max_attempts=max_attempts,
            )
            print(f"\n== {out.name} ({g.sender})")
            for version, check, error in out.attempts:
                if check is None:
                    print(f"   attempt: no script ({error})")
                    continue
                verdict = "PROMOTED" if check.promotable else "rejected"
                print(f"   v{version}: accuracy {check.accuracy:.0%}, {verdict}")
                for f in check.failures[:5]:
                    print(f"      - {f[:300]}")
            promoted += out.promoted is not None
            session.commit()

        rows = repo.promoted_scripts()
        if rows:
            print("\nPromoted scripts now take:")
            for row in rows:
                ran = run_isolated(row.code, [d.doc.text for d in docs], timeout=10 + len(docs))
                if ran["error"]:
                    print(f"   script:{row.name}@{row.version}: didn't run ({ran['error']})")
                    continue
                taken = [d.path for d, r in zip(docs, ran["results"], strict=True) if r["matches"]]
                print(f"   script:{row.name}@{row.version}: {', '.join(taken) or 'nothing stored'}")
            print('Run make e2e ARGS="--no-builtin-scripts --keep-scripts" to extract with them.')
    return 0 if promoted == len(groups) else 1


if __name__ == "__main__":
    sys.exit(main())
