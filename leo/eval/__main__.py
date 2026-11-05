"""`python -m leo.eval` (`make eval`): score stage engines against the answer key (ROADMAP B1).

Always exits 0 on a completed run. The accuracy bar is set in ROADMAP 2.4.
"""

import argparse
import sys
from pathlib import Path

from leo.eval.runner import (
    CLASSIFIERS,
    DEFAULT_CLASSIFIER,
    DEFAULT_DRAFTER,
    DEFAULT_EXTRACTOR,
    DEFAULT_VALIDATOR,
    DRAFTERS,
    EXTRACTORS,
    VALIDATORS,
    format_report,
    run,
)
from leo.fixtures import load_fixture
from leo.jev import JevUnavailable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leo.eval", description=__doc__)
    parser.add_argument("--classifier", choices=sorted(CLASSIFIERS), default=DEFAULT_CLASSIFIER)
    parser.add_argument("--extractor", choices=sorted(EXTRACTORS), default=DEFAULT_EXTRACTOR)
    parser.add_argument(
        "--validator",
        choices=sorted(VALIDATORS),
        default=DEFAULT_VALIDATOR,
        help="validate each extraction before scoring (B4); jev needs JEV_API_KEY",
    )
    parser.add_argument(
        "--drafter",
        choices=sorted(DRAFTERS),
        default=DEFAULT_DRAFTER,
        help="draft an action for each of the key's findings (B7)",
    )
    parser.add_argument(
        "--doc",
        action="append",
        metavar="PATH",
        help="score only this document, as a path under data/synthetic/ (repeatable)",
    )
    parser.add_argument("--json", type=Path, metavar="PATH", help="also write the full result")
    args = parser.parse_args(argv)

    fixture = load_fixture()
    known = {d.path for d in fixture.documents}
    for path in args.doc or []:
        if path not in known:
            parser.error(f"unknown document {path!r}")

    try:
        result = run(
            fixture, args.classifier, args.extractor, args.doc, args.validator, args.drafter
        )
    except JevUnavailable as e:
        parser.error(str(e))
    sys.stdout.write(format_report(result))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(result.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
