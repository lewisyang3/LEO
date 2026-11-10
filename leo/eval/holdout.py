"""`python -m leo.eval.holdout`: the classifier on the held-out set in `data/holdout/`.

The set is five documents written after B2 was tuned, with their classes in `labels.json`. It
has no planted findings, so a document "matters" when its key class isn't noise. Don't tune the
classifier on it (see `data/holdout/README.md`).

Only the classifier runs. Engines are `jev` and `stub` from `leo.eval.runner.CLASSIFIERS`;
`fixture` reads `expected.json`, so it can't classify these documents.
"""

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

from leo.eval.runner import CLASSIFIERS, format_classification
from leo.eval.scoring import (
    ClassificationSummary,
    DocClassification,
    summarize_classification,
)
from leo.interfaces import Classifier, DocumentText
from leo.jev import JevUnavailable
from leo.rules import read_source_text
from leo.types import DocumentClass, LeoModel, SourceDocument

HOLDOUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "holdout"
HOLDOUT_CLASSIFIERS = ("jev", "stub")
STAR_LEGEND = "not noise in the key; this set has no planted findings"


class HoldoutDoc(LeoModel):
    path: str  # relative to the holdout root
    doc_class: DocumentClass
    why: str
    text: DocumentText


class Holdout(LeoModel):
    dataset: str
    documents: list[HoldoutDoc]


class HoldoutResult(LeoModel):
    classifier: str
    dataset: str
    run_at: datetime.datetime
    classification: list[DocClassification]
    classification_summary: ClassificationSummary


def load_holdout(root: Path = HOLDOUT_DIR) -> Holdout:
    """Each document as a stage sees it (path, hash and raw text, unclassified), the same way
    `Fixture.document_text` builds it, plus its key class."""
    labels = json.loads((root / "labels.json").read_text())
    docs = []
    for d in labels["documents"]:
        path = d["path"]
        source = SourceDocument(
            path=path,
            doc_class=None,
            vendor=None,
            sha256=hashlib.sha256((root / path).read_bytes()).hexdigest(),
        )
        text = DocumentText(document=source, text=read_source_text(root / path))
        docs.append(HoldoutDoc(path=path, doc_class=d["class"], why=d["why"], text=text))
    return Holdout(dataset=labels["dataset"], documents=docs)


def run_holdout(holdout: Holdout, clf: Classifier, name: str) -> HoldoutResult:
    """Classify every document. An engine that raises is recorded as an error, never noise."""
    records = getattr(clf, "records", {})
    rows = []
    for d in holdout.documents:
        try:
            result, error = clf.classify(d.text), None
        except Exception as e:
            result, error = None, f"{type(e).__name__}: {e}"
        rows.append(
            DocClassification(
                path=d.path,
                expected=d.doc_class,
                predicted=result.doc_class if result else None,
                confidence=result.confidence if result else None,
                matters=d.doc_class != DocumentClass.noise,
                error=error,
                probabilities=records.get(d.path) if result is not None else None,
            )
        )
    return HoldoutResult(
        classifier=name,
        dataset=holdout.dataset,
        run_at=datetime.datetime.now(datetime.UTC).replace(microsecond=0),
        classification=rows,
        classification_summary=summarize_classification(rows),
    )


def format_holdout(result: HoldoutResult) -> str:
    out = [
        f"LEO holdout eval  classifier={result.classifier}  dataset={result.dataset}"
        f"  {result.run_at.isoformat()}",
        "",
    ]
    out += format_classification(result.classification, result.classification_summary, STAR_LEGEND)
    out += ["", f"n={len(result.classification)}: directional only"]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leo.eval.holdout", description=__doc__)
    parser.add_argument("--classifier", choices=HOLDOUT_CLASSIFIERS, default="jev")
    parser.add_argument("--json", type=Path, metavar="PATH", help="also write the full result")
    args = parser.parse_args(argv)
    try:
        clf = CLASSIFIERS[args.classifier](None)  # type: ignore[arg-type]  # ignores the fixture
    except JevUnavailable as e:
        parser.error(str(e))
    result = run_holdout(load_holdout(), clf, args.classifier)
    sys.stdout.write(format_holdout(result))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(result.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
