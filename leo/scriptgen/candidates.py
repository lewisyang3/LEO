"""Which formats deserve a script.

A format is a sender address. Its examples are the tenant's documents from that sender that
the LLM extracted and that passed validation (`vendor_ok`, no item `needs_review`): the past
verified results a script must reproduce. Documents linked as probable duplicates (A6) don't
count, and a resynced copy is already the same row, so each example is a distinct document.
A sender with at least `threshold` examples is a candidate, unless a promoted stored script
already covers it (its `matcher` is the sender). Hand-written scripts don't count: a run made
with them on never sends their documents to the LLM in the first place.

An LLM answer with items that failed to validate (`item_errors`) isn't an example: a script
that reproduced it would be rejected, so no attempt could pass.

A document with no sender (a contract file) is never an example.

Reviewers' corrections (F3). A correction on a generated script's output demotes the script
(`leo.feedback`), so its sender isn't covered any more. The rewrite's examples are then:
- every document the demoted script extracted, its base output the old script's own `parse`
  (run in the sandbox again), whether or not a reviewer touched it
- documents the LLM extracted, their base output the LLM's answer

Each reviewed item is overlaid on its document's base output: a rejection removes it, a
correction sets the fields it changed. A document whose corrections can't be expressed in the
merged shape (no matching item, or a field code derives) is not an example, and `note` says
why: no script could reproduce it. A document with no correction that the LLM extracted keeps
its answer exactly, so its scriptgen prompt, and the cache, are unchanged.
"""

import copy
import hashlib
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from leo.db.models import Citation, ContractTerm, ExtractorScript, FeedbackEvent, InvoiceFact, Job
from leo.db.models import SourceDocument as DocumentRow
from leo.db.read import _duplicate_documents
from leo.ingest.store import RawStore
from leo.interfaces import Clock, DocumentText, Extraction, Validated
from leo.pipeline.extract_llm import EXTRACTION_PARTS, merge_parts
from leo.pipeline.extract_script import ParseError, sender_address
from leo.rules import read_source_text
from leo.types import DocumentClass, SourceDocument, TermStatus
from leo.worker import queue

DEFAULT_THRESHOLD = 2


@dataclass
class StoredDocument:
    id: UUID
    path: str
    sha256: str
    sender: str
    doc: DocumentText  # LF line ends, as scripts see it
    duplicate: bool
    extract_engine: str | None  # the done extract job's processed_by
    verified: Extraction | None  # the validated extraction, when it passed validation
    llm_output: dict[str, Any] | None  # the LLM's merged answer, when the LLM extracted it
    # What a script's parse(text) must return: the LLM's answer, or with reviewers'
    # corrections the rebuilt output (F3). None when the document can't be an example.
    expected: dict[str, Any] | None = None
    note: str | None = None  # why a reviewed document isn't an example

    def __post_init__(self) -> None:
        if self.expected is None and self.note is None:
            self.expected = self.llm_output

    @property
    def is_example(self) -> bool:
        return (
            bool(self.sender)
            and not self.duplicate
            and self.verified is not None
            and self.expected is not None
        )


@dataclass
class Group:
    sender: str
    examples: list[StoredDocument]  # by sha256, so the prompt is the same every run

    @property
    def vendor_name(self) -> str:
        return self.examples[0].verified.vendor_name or self.sender

    @property
    def examples_hash(self) -> str:
        return hashlib.sha256(":".join(e.sha256 for e in self.examples).encode()).hexdigest()


def _verified(output: dict[str, Any] | None) -> Extraction | None:
    if output is None:
        return None
    v = Validated.model_validate(output)
    x = v.extraction
    items = [*x.terms, *x.invoice_facts]
    if not v.vendor_ok or any(i.status == TermStatus.needs_review for i in items):
        return None
    return x


def _llm_output(output: dict[str, Any] | None, engine: str | None) -> dict[str, Any] | None:
    """The LLM's merged answer, rebuilt from the parts the extract job kept."""
    if output is None or not (engine or "").startswith("llm:"):
        return None
    raw = Extraction.model_validate(output).raw_output or {}
    if raw.get("item_errors"):
        return None
    parts = raw.get("parts") or {}
    outputs = {p: (parts.get(p) or {}).get("output") for p in EXTRACTION_PARTS}
    if any(o is None for o in outputs.values()):
        return None
    return merge_parts(outputs)[0]


def load_documents(
    session: Session, tenant_id: UUID, raw: RawStore, clock: Clock | None = None
) -> list[StoredDocument]:
    """Every document of the tenant, by path, with its extract and validate results, and
    reviewers' corrections applied (F3). `clock` dates the rebuilt extractions (the derived
    deadlines); the configured clock by default."""
    rows = session.scalars(
        select(DocumentRow).where(DocumentRow.tenant_id == tenant_id).order_by(DocumentRow.path)
    ).all()
    jobs: dict[tuple[UUID, str], Job] = {
        (j.document_id, j.stage): j
        for j in session.scalars(
            select(Job).where(
                Job.tenant_id == tenant_id,
                Job.document_id.is_not(None),
                Job.stage.in_((queue.EXTRACT, queue.VALIDATE)),
                Job.status == queue.DONE,
            )
        )
    }
    dupes = set(session.scalars(_duplicate_documents(tenant_id)))
    out: list[StoredDocument] = []
    for row in rows:
        text = read_source_text(raw.path(row.storage_path)).replace("\r\n", "\n")
        doc = DocumentText(
            document=SourceDocument(
                path=row.path,
                doc_class=DocumentClass(row.doc_class) if row.doc_class else None,
                vendor=None if row.vendor_id is None else str(row.vendor_id),
                sha256=row.content_sha256,
            ),
            text=text,
        )
        extract = jobs.get((row.id, queue.EXTRACT))
        validate = jobs.get((row.id, queue.VALIDATE))
        engine = extract.processed_by if extract else None
        out.append(
            StoredDocument(
                id=row.id,
                path=row.path,
                sha256=row.content_sha256,
                sender=sender_address(text),
                doc=doc,
                duplicate=row.id in dupes,
                extract_engine=engine,
                verified=_verified(validate.raw_model_output if validate else None),
                llm_output=_llm_output(extract.raw_model_output if extract else None, engine),
            )
        )
    apply_feedback(session, tenant_id, out, clock)
    return out


# --- Reviewers' corrections (F3) ---------------------------------------------------------------

# Filled only when the document states them, else derived by code: a correction that sets one
# the base output left null can't be reproduced from the text.
_STATED_ONLY = frozenset({"first_application", "initial_term_end"})


def _number(old: Any, new: Any) -> Any:
    """Corrections store amounts as decimal strings; the merged shape has numbers."""
    if isinstance(old, int | float) and not isinstance(old, bool) and isinstance(new, str):
        try:
            return float(new)
        except ValueError:
            return new
    return new


def _overlay(base: dict[str, Any], reviewed: list[tuple]) -> str | None:
    """Apply reviewed items to a merged output in place. Each entry is (kind, type, first
    quote, rejected, [(field, new value), ...]). Returns why it can't, or None."""
    for kind, term_type, quote, rejected, fixes in reviewed:
        items = base.get("terms" if kind == "term" else "invoice_facts") or []
        match = next(
            (
                x
                for x in items
                if isinstance(x, dict)
                and (x.get("quotes") or [None])[0] == quote
                and (kind != "term" or x.get("type") == term_type)
            ),
            None,
        )
        if match is None:
            return f"no {term_type or 'invoice fact'} quoting {quote!r} in the base output"
        if rejected:
            items.remove(match)
            continue
        for field, new in fixes:
            if field.startswith("value."):
                key, target = field.removeprefix("value."), match.get("value")
                if not isinstance(target, dict) or key not in target:
                    return f"{field} isn't in the extraction's output: code derives it"
                if key in _STATED_ONLY and target[key] is None:
                    return f"{field} is derived by code for this document"
            else:
                key, target = field, match
                if key not in target:
                    return f"{field} isn't in the extraction's output"
            target[key] = _number(target[key], new)
    return None


def _reviewed(session: Session, tenant_id: UUID) -> dict[UUID, list[tuple]]:
    """Reviewed (corrected or rejected) items by document, each with its latest fixes."""
    out: dict[UUID, list[tuple]] = defaultdict(list)
    for kind, model, owner in (
        ("term", ContractTerm, Citation.term_id),
        ("invoice_fact", InvoiceFact, Citation.invoice_fact_id),
    ):
        rows = session.execute(
            select(model, Citation.quote)
            .join(Citation, (owner == model.id) & (Citation.ordinal == 0))
            .where(model.tenant_id == tenant_id, model.corrected_at.is_not(None))
            .order_by(model.id)
        ).all()
        for row, quote in rows:
            col = FeedbackEvent.term_id if kind == "term" else FeedbackEvent.invoice_fact_id
            events = session.scalars(
                select(FeedbackEvent)
                .where(col == row.id, FeedbackEvent.event_type == "correct")
                .order_by(FeedbackEvent.created_at, FeedbackEvent.id)
            ).all()
            fixes = {e.field: e.new_value for e in events if e.field}
            out[row.document_id].append(
                (
                    kind,
                    getattr(row, "type", None),
                    quote,
                    row.status == TermStatus.rejected,
                    sorted(fixes.items()),
                )
            )
    return out


def apply_feedback(
    session: Session, tenant_id: UUID, docs: Sequence[StoredDocument], clock: Clock | None
) -> None:
    """Rebuild `expected` and `verified` where reviewers' corrections reach (see the module
    docstring). Untouched documents keep the LLM's answer as it is."""
    from leo.clock import get_clock
    from leo.scriptgen.sandbox import run_isolated
    from leo.scriptgen.scripts import to_extraction

    demoted = {
        f"script:{r.name}@{r.version}": r
        for r in session.scalars(
            select(ExtractorScript).where(
                ExtractorScript.tenant_id == tenant_id, ExtractorScript.demoted_at.is_not(None)
            )
        )
    }
    reviewed = _reviewed(session, tenant_id)
    if not demoted and not reviewed:
        return
    clock = clock or get_clock()

    base: dict[UUID, dict[str, Any] | None] = {}
    by_script: dict[str, list[StoredDocument]] = defaultdict(list)
    for d in docs:
        if (d.extract_engine or "") in demoted:
            by_script[d.extract_engine].append(d)
        elif d.id in reviewed and d.llm_output is not None:
            base[d.id] = copy.deepcopy(d.llm_output)
    for engine, mine in by_script.items():
        ran = run_isolated(demoted[engine].code, [d.doc.text for d in mine], timeout=10 + len(mine))
        for d, r in zip(mine, ran["results"] or [None] * len(mine), strict=True):
            ok = not ran["error"] and r and r["matches"] and not r["error"]
            base[d.id] = r["output"] if ok else None
            if not ok:
                d.expected, d.note = None, f"{engine} no longer parses it"

    for d in docs:
        if d.id not in base or base[d.id] is None:
            continue
        out = base[d.id]
        if why := _overlay(out, reviewed.get(d.id, [])):
            d.expected, d.note = None, why
            continue
        if d.verified is None:  # it never passed validation: not a verified result
            d.expected = None
            continue
        try:
            d.verified = to_extraction(d.doc, out, clock)
        except ParseError as e:
            d.expected, d.note = None, f"the corrected output doesn't convert: {e}"
            continue
        d.expected = out


def find_groups(
    docs: Sequence[StoredDocument],
    threshold: int = DEFAULT_THRESHOLD,
    covered: Collection[str] = (),
) -> list[Group]:
    """Senders with at least `threshold` verified LLM examples, other than the `covered`
    senders (the matchers of the promoted stored scripts). By sender."""
    by_sender: dict[str, dict[str, StoredDocument]] = defaultdict(dict)
    for d in docs:
        if d.is_example:
            by_sender[d.sender].setdefault(d.sha256, d)
    groups: list[Group] = []
    for sender in sorted(by_sender):
        examples = [by_sender[sender][sha] for sha in sorted(by_sender[sender])]
        if len(examples) < threshold or sender in covered:
            continue
        groups.append(Group(sender, examples))
    return groups


def negatives(docs: Sequence[StoredDocument], group: Group) -> list[StoredDocument]:
    """The documents a script for `group` must not match: every other sender's, by path."""
    return [d for d in docs if d.sender != group.sender]
