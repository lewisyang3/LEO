"""Testing a proposed script and promoting it.

A candidate is promoted only if it matches past verified results:

- **Positive.** On every example it matches, and its output, converted the way the LLM's is
  (`leo.scriptgen.scripts.to_extraction`), equals the verified extraction. Items compare as
  order-free lists, without ids, vendor ids, document paths, statuses, notes and quotes (a
  script quotes whole lines, which needn't be the LLM's spans). Every quote must still be one
  line of the text.
- **Negative.** It matches no document from another sender, noise and contracts included.

`accuracy` is the share of examples that pass. Promotion needs 1.0 and no false match. Every
candidate is stored in `extractor_script`, promoted or not, so a rejected one stays as the
test record. A failed attempt's messages go into the next attempt's prompt, up to
`generate.MAX_ATTEMPTS`.

The candidate runs in a spawned child with a time limit (`sandbox.run_isolated`).
"""

import datetime
import enum
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from leo.db.repo import Repo
from leo.interfaces import Clock, Extraction
from leo.llm import ResponseCache
from leo.match.names import match_form
from leo.pipeline.extract_script import ParseError
from leo.scriptgen import generate
from leo.scriptgen.candidates import Group, StoredDocument
from leo.scriptgen.sandbox import run_isolated
from leo.scriptgen.scripts import to_extraction

IGNORED = {"id", "vendor", "document", "status", "notes", "quotes", "quote", "matches_transaction"}


def _plain(v: Any) -> Any:
    """Decimals compare by value (1308.0 == 1308.00), dates as ISO strings."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items() if k not in IGNORED}
    if isinstance(v, list | tuple):
        return [_plain(x) for x in v]
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, enum.Enum):
        return v.value
    return v


def _items(items: Sequence[BaseModel]) -> list[str]:
    return sorted(json.dumps(_plain(i.model_dump()), sort_keys=True, default=str) for i in items)


def compare(expected: Extraction, got: Extraction) -> list[str]:
    """Where `got` differs from `expected`, one message per field list; empty when equal."""
    diffs: list[str] = []
    if expected.vendor_name != got.vendor_name:
        diffs.append(f"vendor_name: expected {expected.vendor_name!r}, got {got.vendor_name!r}")
    for kind in ("terms", "invoice_facts", "contacts"):
        want, have = _items(getattr(expected, kind)), _items(getattr(got, kind))
        if want != have:
            diffs.append(f"{kind}: expected {want}, got {have}")
    return diffs


@dataclass
class CandidateCheck:
    accuracy: float
    failures: list[str] = field(default_factory=list)
    false_matches: list[str] = field(default_factory=list)

    @property
    def promotable(self) -> bool:
        return self.accuracy == 1.0 and not self.false_matches and not self.failures


def check_candidate(
    code: str,
    group: Group,
    others: Sequence[StoredDocument],
    clock: Clock,
    run: Callable[..., dict[str, Any]] = run_isolated,
) -> CandidateCheck:
    """The positive and negative tests. Messages name documents by path, never by anything
    that changes between runs, because they go into the retry prompt."""
    texts = [e.doc.text for e in group.examples] + [d.doc.text for d in others]
    ran = run(code, texts, timeout=10.0 + len(texts))
    if ran["error"]:
        return CandidateCheck(0.0, [f"the module didn't run: {ran['error']}"])
    results = ran["results"]
    failures: list[str] = []
    passed = 0
    for ex, r in zip(group.examples, results, strict=False):
        name = ex.path
        if not r["matches"]:
            failures.append(f"{name}: matches() returned False on an example")
            continue
        if r["error"]:
            failures.append(f"{name}: parse() raised {r['error']}")
            continue
        try:
            got = to_extraction(ex.doc, r["output"], clock)
        except ParseError as e:
            failures.append(f"{name}: {e}")
            continue
        if diffs := compare(ex.verified, got):
            failures += [f"{name}: {d}" for d in diffs]
            continue
        passed += 1
    false_matches = [
        f"{d.path}: matches() returned True on another sender's document"
        for d, r in zip(others, results[len(group.examples) :], strict=False)
        if r["matches"]
    ]
    return CandidateCheck(passed / len(group.examples), failures + false_matches, false_matches)


def script_name(group: Group) -> str:
    return "gen-" + "-".join(match_form(group.vendor_name).split())


@dataclass
class Outcome:
    group: Group
    name: str
    attempts: list[tuple[str | None, CandidateCheck | None, str | None]]  # version, test, error
    promoted: str | None = None  # the promoted version


def generate_and_promote(
    repo: Repo,
    group: Group,
    others: Sequence[StoredDocument],
    clock: Clock,
    cache: ResponseCache,
    settings=None,
    client=None,
    max_attempts: int = generate.MAX_ATTEMPTS,
    run: Callable[..., dict[str, Any]] = run_isolated,
) -> Outcome:
    """Up to `max_attempts` proposals for one group; stops at the first that passes."""
    name = script_name(group)
    outcome = Outcome(group, name, [])
    previous: list[generate.Attempt] = []
    for _ in range(max_attempts):
        proposal, result = generate.propose(group, previous, cache, settings, client)
        if proposal is None:
            outcome.attempts.append((None, None, result.record.get("error", "no answer")))
            break
        test = check_candidate(proposal.code, group, others, clock, run=run)
        row = repo.add_extractor_script(
            name,
            proposal.code,
            matcher=group.sender,
            accuracy=test.accuracy,
            promoted_at=datetime.datetime.now(datetime.UTC) if test.promotable else None,
        )
        outcome.attempts.append((row.version, test, None))
        if test.promotable:
            outcome.promoted = row.version
            break
        previous.append(generate.Attempt(proposal.code, test.failures))
    return outcome
