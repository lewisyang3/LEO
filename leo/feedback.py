"""The feedback loop: a reviewer corrects or rejects an extracted item.

A correction changes the stored term or invoice fact, sets its `corrected_at` (so
re-extraction never overwrites it, `Repo.upsert_term`) and writes one `feedback_event` per
changed field. A rejection sets the item's status to `rejected`, which every consumer of the
ledger leaves out (`EXCLUDED_STATUSES`). Each event records the item's document and the extract
engine that produced it, so later stages can use the corrections as examples for the
extraction prompt or trace them to a generated script.

Only an item's values are editable. Its type and quotes make its natural key, so they stay.
Either change asks for a reconcile round, so findings and unedited drafts follow.
"""

import datetime
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from leo.db.models import (
    Citation,
    ContractTerm,
    DocumentLink,
    ExtractorScript,
    FeedbackEvent,
    InvoiceFact,
    Job,
)
from leo.db.write import jsonb
from leo.match.duplicates import PROBABLE_DUPLICATE
from leo.pipeline.extract_schema import EXAMPLE_FIELDS, INVOICE_FACT, PART_OF_ITEM
from leo.pipeline.validate import billing_period_ok
from leo.types import TermStatus, extracted_term_adapter
from leo.worker import queue

CORRECT = "correct"
REJECT = "reject"

TERM_FIELDS = ("value", "effective_date", "plan_label", "agreement_ref")
INVOICE_FIELDS = ("amount", "billing_period", "invoice_date", "invoice_number", "seats_billed")


class ItemNotFound(KeyError):
    pass


class BadCorrection(ValueError):
    """The edit doesn't make a valid item (HTTP 422)."""


class AlreadyRejected(ValueError):
    """A rejected item can't be corrected or rejected again (HTTP 409)."""


def _engine(session: Session, tenant_id: UUID, document_id: UUID) -> str | None:
    """The extract engine that produced the document's items: its extract job's
    `processed_by` (`llm:<model>`, `script:<name>@<version>`, ...)."""
    return session.scalar(
        select(Job.processed_by).where(
            Job.tenant_id == tenant_id,
            Job.document_id == document_id,
            Job.stage == queue.EXTRACT,
        )
    )


def _row(session: Session, model, tenant_id: UUID, item_id: UUID):
    row = session.scalar(
        select(model).where(model.id == item_id, model.tenant_id == tenant_id).with_for_update()
    )
    if row is None:
        raise ItemNotFound(str(item_id))
    if row.status == TermStatus.rejected:
        raise AlreadyRejected(f"{model.__tablename__} {item_id} is rejected")
    return row


def _event(row, owner: str, event_type: str, engine: str | None, reviewer: str, **cols):
    return FeedbackEvent(
        tenant_id=row.tenant_id,
        event_type=event_type,
        document_id=row.document_id,
        engine=engine,
        reviewer=reviewer,
        **{owner: row.id},
        **cols,
    )


def _term_value(term_type: str, value: Any) -> dict:
    """The value checked against its type's value model, as stored (JSON-ready)."""
    probe = {
        "id": "x",
        "vendor": "",
        "document": "",
        "quotes": ["x"],
        "type": term_type,
        "value": value,
    }
    try:
        term = extracted_term_adapter.validate_python(probe)
    except ValidationError as e:
        raise BadCorrection(_errors(e)) from None
    return jsonb(term.value)


def _errors(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())


def _finish(session: Session, row, events: list[FeedbackEvent]) -> None:
    row.corrected_at = func.now()
    if row.status == TermStatus.needs_review:
        row.status = TermStatus.current  # a reviewer has now checked it
    session.add_all(events)
    queue.request_reconcile(session, row.tenant_id)
    session.flush()
    _demote(session, row.tenant_id, events[0].engine)


# --- Demoting a generated script ---------------------------------------------------------

GENERATED = "script:gen-"


def _script(session: Session, tenant_id: UUID, engine: str | None) -> ExtractorScript | None:
    """The stored generated script an engine string names (`script:<name>@<version>`, as
    `leo.scriptgen.scripts` writes it). None for the LLM, hand-written scripts and the rest."""
    if not engine or not engine.startswith(GENERATED):
        return None
    name, _, version = engine.removeprefix("script:").rpartition("@")
    return session.scalar(
        select(ExtractorScript).where(
            ExtractorScript.tenant_id == tenant_id,
            ExtractorScript.name == name,
            ExtractorScript.version == version,
        )
    )


def _demote(session: Session, tenant_id: UUID, engine: str | None) -> None:
    """A reviewed item lowers its generated script's accuracy: the share of the documents
    this version extracted that have no reviewed item. Below 1.0, the promotion bar, the
    script is demoted: the worker sends its sender to the LLM, and scriptgen may rewrite it."""
    script = _script(session, tenant_id, engine)
    if script is None:
        return
    handled = set(
        session.scalars(
            select(Job.document_id).where(
                Job.tenant_id == tenant_id,
                Job.stage == queue.EXTRACT,
                Job.status == queue.DONE,
                Job.processed_by == engine,
            )
        )
    )
    if not handled:
        return
    reviewed: set[UUID] = set()
    for model in (ContractTerm, InvoiceFact):
        reviewed |= set(
            session.scalars(
                select(model.document_id).where(
                    model.tenant_id == tenant_id,
                    model.document_id.in_(handled),
                    model.corrected_at.is_not(None),
                )
            )
        )
    accuracy = (len(handled) - len(reviewed)) / len(handled)
    script.accuracy = Decimal(str(round(accuracy, 4)))
    if accuracy < 1 and script.demoted_at is None:
        script.demoted_at = func.now()
    session.flush()


def demoted_script(session: Session, tenant_id: UUID, engine: str | None) -> str | None:
    """`name@version` when the engine is a generated script that is now demoted."""
    script = _script(session, tenant_id, engine)
    if script is None or script.demoted_at is None:
        return None
    return f"{script.name}@{script.version}"


def correct_term(
    session: Session, tenant_id: UUID, reviewer: str, term_id: UUID, edits: Mapping[str, Any]
) -> list[FeedbackEvent]:
    """Apply `edits` (a subset of `TERM_FIELDS`; `value` may be partial: its keys replace the
    stored value's) to one term. One event per changed field (`value.<key>` for value keys).
    Returns the events; nothing changed means no events and no write."""
    if unknown := set(edits) - set(TERM_FIELDS):
        raise BadCorrection(f"not editable: {', '.join(sorted(unknown))}")
    row = _row(session, ContractTerm, tenant_id, term_id)
    engine = _engine(session, tenant_id, row.document_id)
    changes: list[tuple[str, Any, Any]] = []
    if "value" in edits:
        if not isinstance(edits["value"], Mapping):
            raise BadCorrection("value: must be an object")
        new_value = _term_value(row.type, {**row.value, **edits["value"]})
        # Only the keys the reviewer sent: re-validation may reformat others.
        changes += [
            (f"value.{k}", row.value.get(k), new_value[k])
            for k in edits["value"]
            if k in new_value and row.value.get(k) != new_value[k]
        ]
        if new_value != row.value:
            row.value = new_value
    if "effective_date" in edits:
        new_date = _date(edits["effective_date"], "effective_date")
        if new_date != row.effective_date:
            changes.append(("effective_date", jsonb(row.effective_date), jsonb(new_date)))
            row.effective_date = new_date
    for col in ("plan_label", "agreement_ref"):
        if col in edits:
            new = _text(edits[col])
            if new != getattr(row, col):
                changes.append((col, getattr(row, col), new))
                setattr(row, col, new)
    events = [
        _event(row, "term_id", CORRECT, engine, reviewer, field=f, old_value=o, new_value=n)
        for f, o, n in changes
    ]
    if events:
        _finish(session, row, events)
    return events


def correct_invoice_fact(
    session: Session, tenant_id: UUID, reviewer: str, fact_id: UUID, edits: Mapping[str, Any]
) -> list[FeedbackEvent]:
    """Apply `edits` (a subset of `INVOICE_FIELDS`) to one invoice fact, one event per changed
    field. A corrected fact becomes `current`, so its billing period must parse."""
    if unknown := set(edits) - set(INVOICE_FIELDS):
        raise BadCorrection(f"not editable: {', '.join(sorted(unknown))}")
    row = _row(session, InvoiceFact, tenant_id, fact_id)
    engine = _engine(session, tenant_id, row.document_id)
    new: dict[str, Any] = {}
    for col, v in edits.items():
        if col == "amount":
            new[col] = _amount(v)
        elif col == "invoice_date":
            new[col] = _date(v, col)
            if new[col] is None:
                raise BadCorrection("invoice_date: required")
        elif col == "billing_period":
            new[col] = _text(v)
            if new[col] is None:
                raise BadCorrection("billing_period: required")
        elif col == "seats_billed":
            new[col] = _seats(v)
        else:
            new[col] = _text(v)
    period = new.get("billing_period", row.billing_period)
    if not billing_period_ok(period):
        raise BadCorrection(f"billing_period {period!r} is not YYYY-MM or a start/end range")
    changes = [(c, getattr(row, c), v) for c, v in new.items() if getattr(row, c) != v]
    for c, _, v in changes:
        setattr(row, c, v)
    events = [
        _event(
            row,
            "invoice_fact_id",
            CORRECT,
            engine,
            reviewer,
            field=c,
            old_value=jsonb(o),
            new_value=jsonb(v),
        )
        for c, o, v in changes
    ]
    if events:
        _finish(session, row, events)
    return events


def reject(
    session: Session, tenant_id: UUID, reviewer: str, kind: str, item_id: UUID
) -> FeedbackEvent:
    """`kind` is `term` or `invoice_fact`: the item isn't one. Its status becomes `rejected`."""
    model, owner = {
        "term": (ContractTerm, "term_id"),
        "invoice_fact": (InvoiceFact, "invoice_fact_id"),
    }[kind]
    row = _row(session, model, tenant_id, item_id)
    engine = _engine(session, tenant_id, row.document_id)
    event = _event(
        row,
        owner,
        REJECT,
        engine,
        reviewer,
        field="status",
        old_value=row.status,
        new_value=TermStatus.rejected.value,
    )
    row.status = TermStatus.rejected
    if kind == "term":
        row.superseded_by = row.supersedes = None
    _finish(session, row, [event])
    return event


# --- Parsing the edited values ---------------------------------------------------------------


def _text(v: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise BadCorrection(f"expected text, got {v!r}")
    return v.strip() or None


def _date(v: Any, field: str) -> datetime.date | None:
    if v is None or v == "":
        return None
    try:
        return datetime.date.fromisoformat(str(v))
    except ValueError:
        raise BadCorrection(f"{field}: {v!r} is not a YYYY-MM-DD date") from None


def _amount(v: Any) -> Decimal:
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        raise BadCorrection(f"amount: {v!r} is not a number") from None
    if not d.is_finite():
        raise BadCorrection(f"amount: {v!r} is not a number")
    return d


def _seats(v: Any) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, int | str) or not str(v).strip().isdigit():
        raise BadCorrection(f"seats_billed: {v!r} is not a whole number")
    return int(v)


# --- Few-shot examples for the extraction prompt ------------------------------------------

FEW_SHOTS_PER_PART = 5


@dataclass(frozen=True)
class FewShot:
    """One reviewed item, as an example for the part that extracts its kind."""

    item: str  # a term type, or "invoice_fact"
    quotes: tuple[str, ...]  # the item's citations, in order
    fixes: tuple[tuple[str, Any, Any], ...]  # (field, extracted, corrected); empty if rejected
    rejected: bool
    event_ids: tuple[str, ...]


def _related_documents(session: Session, tenant_id: UUID, document_id: UUID) -> set[UUID]:
    """The document and those linked to it as a probable duplicate, either way round."""
    rows = session.execute(
        select(DocumentLink.document_id, DocumentLink.linked_document_id).where(
            DocumentLink.tenant_id == tenant_id,
            DocumentLink.link_type == PROBABLE_DUPLICATE,
            or_(
                DocumentLink.document_id == document_id,
                DocumentLink.linked_document_id == document_id,
            ),
        )
    ).all()
    return {document_id, *(d for r in rows for d in r)}


def few_shots(
    session: Session,
    tenant_id: UUID,
    *,
    exclude_document: UUID,
    limit: int = FEW_SHOTS_PER_PART,
) -> dict[str, list[FewShot]]:
    """The tenant's reviewed items as examples, by extraction part (`EXTRACTION_PARTS` names),
    newest first, at most `limit` per part.

    Left out: items from the document being extracted and its linked duplicates (it shouldn't
    see its own answer), and fixes to fields the model doesn't write (`EXAMPLE_FIELDS`). An
    item's latest state wins: a rejection after corrections is one rejection, and a field
    corrected twice shows its first extracted and last corrected value. Same events, same
    examples, in the same order, so a rerun hits the response cache."""
    skip = _related_documents(session, tenant_id, exclude_document)
    events = session.scalars(
        select(FeedbackEvent)
        .where(
            FeedbackEvent.tenant_id == tenant_id,
            or_(FeedbackEvent.term_id.is_not(None), FeedbackEvent.invoice_fact_id.is_not(None)),
            FeedbackEvent.document_id.not_in(skip),
        )
        .order_by(FeedbackEvent.created_at, FeedbackEvent.id)
    ).all()
    by_item: dict[tuple[str, UUID], list[FeedbackEvent]] = defaultdict(list)
    for e in events:
        key = ("term", e.term_id) if e.term_id is not None else (INVOICE_FACT, e.invoice_fact_id)
        by_item[key].append(e)
    if not by_item:
        return {}

    term_ids = [i for k, i in by_item if k == "term"]
    types = dict(
        session.execute(
            select(ContractTerm.id, ContractTerm.type).where(ContractTerm.id.in_(term_ids))
        ).all()
    )
    owners = [i for _, i in by_item]
    quotes: dict[UUID, list[str]] = defaultdict(list)
    for term_id, fact_id, quote in session.execute(
        select(Citation.term_id, Citation.invoice_fact_id, Citation.quote)
        .where(
            Citation.tenant_id == tenant_id,
            or_(Citation.term_id.in_(owners), Citation.invoice_fact_id.in_(owners)),
        )
        .order_by(Citation.ordinal)
    ):
        quotes[term_id or fact_id].append(quote)

    # Newest item first: by its latest event.
    order = sorted(by_item, key=lambda k: (by_item[k][-1].created_at, by_item[k][-1].id))
    out: dict[str, list[FewShot]] = defaultdict(list)
    for key in reversed(order):
        evs = by_item[key]
        item = types.get(key[1]) if key[0] == "term" else INVOICE_FACT
        if item is None or item not in PART_OF_ITEM:
            continue
        part = PART_OF_ITEM[item]
        if len(out.get(part, ())) >= limit:
            continue
        rejected = evs[-1].event_type == REJECT
        fixes: dict[str, list[Any]] = {}
        if not rejected:
            for e in evs:
                if e.event_type != CORRECT or e.field not in EXAMPLE_FIELDS[item]:
                    continue
                if e.field in fixes:
                    fixes[e.field][1] = e.new_value
                else:
                    fixes[e.field] = [e.old_value, e.new_value]
            fixes = {f: v for f, v in fixes.items() if v[0] != v[1]}
            if not fixes:
                continue
        out[part].append(
            FewShot(
                item=item,
                quotes=tuple(quotes.get(key[1], ())),
                fixes=tuple((f, o, n) for f, (o, n) in fixes.items()),
                rejected=rejected,
                event_ids=tuple(str(e.id) for e in evs),
            )
        )
    return dict(out)
