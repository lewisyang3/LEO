"""The accuracy bar (ROADMAP 2.4), checked on an eval result by `make bar`.

The bar is: every planted finding (F1-F5), no false finding, and at least 90% of the key's
term fields right. `make bar` checks the extraction half here, on the script and cached LLM
extractors; `make e2e` checks the findings end to end (F1-F5, N1-N8, no extra finding).
"""

from leo.eval.runner import EvalResult
from leo.types import LeoModel

BAR_TERM_FIELDS = 0.90


class BarCheck(LeoModel):
    id: str
    target: str
    actual: str
    ok: bool


def check_bar(result: EvalResult) -> list[BarCheck]:
    checks = [
        BarCheck(
            id="extractor",
            target="not fixture",
            actual=result.extractor,
            # The key scored against itself proves nothing.
            ok=result.extractor != "fixture",
        )
    ]
    fs = result.findings
    if fs is None:
        checks += [
            BarCheck(id="findings", target="5/5", actual="not run (--reconciler none)", ok=False),
            BarCheck(id="false findings", target="0", actual="not run", ok=False),
        ]
    else:
        right = sum(f.ok for f in fs.expected)
        checks += [
            BarCheck(
                id="findings",
                target=f"{len(fs.expected)}/{len(fs.expected)}",
                actual=f"{right}/{len(fs.expected)}",
                ok=right == len(fs.expected),
            ),
            BarCheck(
                id="false findings",
                target="0",
                actual=str(len(fs.extra)),
                ok=not fs.extra,
            ),
        ]
    acc = result.extraction_summary.terms.field_accuracy
    rate = acc.rate()
    checks.append(
        BarCheck(
            id="term fields",
            target=f">= {BAR_TERM_FIELDS:.0%}",
            actual=f"{acc.hit}/{acc.total}" + ("" if rate is None else f" ({rate:.1%})"),
            ok=rate is not None and rate >= BAR_TERM_FIELDS,
        )
    )
    return checks


def format_bar(checks: list[BarCheck]) -> str:
    out = ["", "== Accuracy bar (ROADMAP 2.4) =="]
    for c in checks:
        out.append(f" {'PASS' if c.ok else 'FAIL'}  {c.id:<16} {c.actual}  (target {c.target})")
    failed = [c.id for c in checks if not c.ok]
    out.append(f"bar missed: {', '.join(failed)}" if failed else "bar met")
    return "\n".join(out) + "\n"
