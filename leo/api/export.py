"""CSV export of the term record and the findings (ROADMAP C4).

Pure formatting: `DbStore` loads the rows (through `leo.db.read`) and these functions turn them
into CSV text. Both files start with a UTF-8 BOM so Excel reads non-ASCII text correctly.

The term record has one row per stored term, in every status, so supersessions and the price
history show. Term values differ by type, so each value key gets its own column (the union of
the `*Value` models' fields), blank where a type has no such key.

Quotes and titles come from vendor emails, so a text cell that a spreadsheet would read as a
formula (it starts with `= + - @`, a tab or a CR) gets a leading `'`. Numbers are never
prefixed, so negative amounts stay numeric.
"""

import csv
import datetime
import io
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

from leo.api.schemas import ActionOut, FindingSummary
from leo.interfaces import Ledger
from leo.types import (
    DisputeWindowValue,
    EscalatorValue,
    Finding,
    NoticeWindowValue,
    PriceCapValue,
    PriceValue,
    RenewalValue,
    SeatCountValue,
)

BOM = "﻿"
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
JOIN = " | "

_VALUE_MODELS = (
    PriceValue,
    PriceCapValue,
    EscalatorValue,
    RenewalValue,
    NoticeWindowValue,
    SeatCountValue,
    DisputeWindowValue,
)
VALUE_COLUMNS: list[str] = list(dict.fromkeys(k for v in _VALUE_MODELS for k in v.model_fields))

TERM_COLUMNS = [
    "vendor",
    "type",
    "plan_label",
    "agreement_ref",
    "effective_date",
    "status",
    *VALUE_COLUMNS,
    "old_amount",
    "document",
    "quotes",
    "notes",
    "id",
    "supersedes",
    "superseded_by",
]

FINDING_COLUMNS = [
    "vendor",
    "kind",
    "title",
    "plan_label",
    "deadline",
    "recommended_send_by",
    "days_left",
    "at_stake",
    "impact",
    "computation",
    "action_type",
    "action_state",
    "documents",
    "linked_duplicates",
    "id",
]


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Enum):
        v = v.value
    if isinstance(v, (Decimal, int, float, datetime.date)):
        return v.isoformat() if isinstance(v, datetime.date) else str(v)
    s = str(v)
    return "'" + s if s.startswith(FORMULA_START) else s


def _pairs(model: BaseModel | None) -> str:
    """`key=value; …` for an impact or computation, whose fields differ by finding kind."""
    if model is None:
        return ""
    return "; ".join(f"{k}={_cell(getattr(model, k))}" for k in type(model).model_fields)


def _write(columns: list[str], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in columns])
    return BOM + buf.getvalue()


def terms_csv(ledger: Ledger) -> str:
    names = {v.id: v.canonical_name for v in ledger.vendors}

    def key(t):
        d = t.effective_date
        return (names.get(t.vendor, ""), t.type.value, d is None, d or datetime.date.min, t.id)

    rows = []
    for t in sorted(ledger.terms, key=key):
        rows.append(
            {
                "vendor": names.get(t.vendor, t.vendor),
                "type": t.type,
                "plan_label": t.plan_label,
                "agreement_ref": t.agreement_ref,
                "effective_date": t.effective_date,
                "status": t.status,
                **{k: getattr(t.value, k) for k in type(t.value).model_fields},
                "old_amount": t.old_value.amount if t.old_value else None,
                "document": t.document,
                "quotes": JOIN.join(t.quotes),
                "notes": t.notes,
                "id": t.id,
                "supersedes": t.supersedes,
                "superseded_by": t.superseded_by,
            }
        )
    return _write(TERM_COLUMNS, rows)


def findings_csv(
    summaries: list[FindingSummary],
    findings: list[Finding],
    actions: dict[str, ActionOut],
) -> str:
    """One row per summary, in the summaries' order (the dashboard's)."""
    by_id = {f.id: f for f in findings}
    rows = []
    for s in summaries:
        f = by_id[s.id]
        a = actions[s.id]
        rows.append(
            {
                "vendor": s.vendor_name,
                "kind": f.kind,
                "title": f.title,
                "plan_label": f.plan_label,
                "deadline": f.deadline,
                "recommended_send_by": f.recommended_send_by,
                "days_left": s.days_left,
                "at_stake": s.at_stake,
                "impact": _pairs(f.impact),
                "computation": _pairs(getattr(f, "computation", None)),
                "action_type": a.type,
                "action_state": a.state,
                "documents": JOIN.join(f.documents),
                "linked_duplicates": JOIN.join(f.linked_duplicates),
                "id": f.id,
            }
        )
    return _write(FINDING_COLUMNS, rows)
