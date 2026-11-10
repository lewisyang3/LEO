"""The feedback loop (ROADMAP Phase 4): a reviewer corrects or rejects an extracted item.

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
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leo.db.models import ContractTerm, FeedbackEvent, InvoiceFact, Job
from leo.db.write import jsonb
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
