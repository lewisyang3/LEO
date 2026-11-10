"""Which formats deserve a script (ROADMAP Phase 4).

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
"""

import hashlib
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from leo.db.models import Job
from leo.db.models import SourceDocument as DocumentRow
from leo.db.read import _duplicate_documents
from leo.ingest.store import RawStore
from leo.interfaces import DocumentText, Extraction, Validated
from leo.pipeline.extract_llm import EXTRACTION_PARTS, merge_parts
from leo.pipeline.extract_script import sender_address
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

    @property
    def is_example(self) -> bool:
        return (
            bool(self.sender)
            and not self.duplicate
            and self.verified is not None
            and self.llm_output is not None
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


def load_documents(session: Session, tenant_id: UUID, raw: RawStore) -> list[StoredDocument]:
    """Every document of the tenant, by path, with its extract and validate results."""
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
    return out


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
