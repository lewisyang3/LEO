"""Duplicate linking (ROADMAP A6).

Each document stores a `context_fingerprint`: a hash of its stored terms and invoice facts
(vendor, type, value, plan, agreement, effective date; invoice number, billing period, invoice
date, amount). Quotes, offsets, ids, status and notes are left out, so a forward of the same
notice fingerprints the same. A document with no items has no fingerprint.

The fingerprint is never a key and never deletes anything. It only suggests links, per the
design doc's table, narrowed because the dataset has no attachments:

- same fingerprint, and one side is a forward: the forward is a `probable_duplicate` of the other
- same fingerprint, and the two emails carry the same attachment: the later is a
  `probable_duplicate` of the earlier
- anything else stays separate (a recurring invoice differs on number or period anyway)

`link_duplicates` recomputes from what is stored: it upserts the links it finds and deletes the
`probable_duplicate` links it no longer finds, so a rerun gives the same rows. Readers leave
a linked duplicate's items out, so its facts count once, under the original.
"""

import datetime as dt
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic_core import to_jsonable_python
from sqlalchemy import select

from leo.db.keys import is_eml
from leo.db.models import ContractTerm, DocumentLink, InvoiceFact, SourceDocument
from leo.db.repo import Repo
from leo.ingest.eml import parse_eml
from leo.ingest.ingest import ATTACHMENT_LINK
from leo.ingest.store import RawStore
from leo.rules import read_source_text

PROBABLE_DUPLICATE = "probable_duplicate"

_FWD_SUBJECT = re.compile(r"^\s*fwd?\s*:", re.IGNORECASE)
_NUMBER = re.compile(r"-?\d+(\.\d+)?")
_FWD_BLOCK = re.compile(
    r"^-{2,}\s*(forwarded message|original message)\s*-{2,}\s*$", re.IGNORECASE | re.MULTILINE
)


def _canon(item: Sequence[Any]) -> str:
    return json.dumps(to_jsonable_python(list(item)), sort_keys=True, separators=(",", ":"))


def context_fingerprint(
    terms: Iterable[Sequence[Any]], facts: Iterable[Sequence[Any]]
) -> str | None:
    """`v1:` + SHA-256 over the sorted canonical JSON of each item. Items are tuples: terms
    `(vendor, type, value, plan_label, agreement_ref, effective_date)`, facts `(vendor,
    invoice_number, billing_period, invoice_date, amount)`. Each item becomes a JSON string
    before sorting, because values are dicts and fields may be None. None with no items."""
    items = sorted(["t" + _canon(t) for t in terms] + ["f" + _canon(f) for f in facts])
    if not items:
        return None
    return "v1:" + hashlib.sha256(json.dumps(items).encode()).hexdigest()


def is_forward(subject: str | None, text: str) -> bool:
    """A `Fwd:`/`Fw:` subject, or a forwarded header block in the text."""
    return bool(subject and _FWD_SUBJECT.match(subject)) or bool(_FWD_BLOCK.search(text))


def _norm_amount(x: Any) -> str:
    """Numeric(18,4) reads back as Decimal('339.0000'); normalize so 339 and 339.0000 agree."""
    return format(x.normalize(), "f") if x is not None else "None"


def _norm_value(v: Any) -> Any:
    """A term value's JSON with every number in one form, so two extractors' copies agree:
    `339`, `339.0` and `"339.00"` (how a Decimal is stored) all become `"339"`. Strings that
    aren't numbers, and booleans, are kept."""
    if isinstance(v, dict):
        return {k: _norm_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm_value(x) for x in v]
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int | float | Decimal):
        return _norm_amount(Decimal(str(v)))
    if isinstance(v, str) and _NUMBER.fullmatch(v):
        return _norm_amount(Decimal(v))
    return v


def _fingerprints(repo: Repo) -> dict[UUID, str]:
    s, t = repo.session, repo.tenant_id
    terms: dict[UUID, list[tuple]] = defaultdict(list)
    for r in s.execute(
        select(
            ContractTerm.document_id,
            ContractTerm.vendor_id,
            ContractTerm.type,
            ContractTerm.value,
            ContractTerm.plan_label,
            ContractTerm.agreement_ref,
            ContractTerm.effective_date,
        ).where(ContractTerm.tenant_id == t)
    ):
        doc, vendor, ttype, value, *rest = r
        terms[doc].append((str(vendor), ttype, _norm_value(value), *rest))
    facts: dict[UUID, list[tuple]] = defaultdict(list)
    for r in s.execute(
        select(
            InvoiceFact.document_id,
            InvoiceFact.vendor_id,
            InvoiceFact.invoice_number,
            InvoiceFact.billing_period,
            InvoiceFact.invoice_date,
            InvoiceFact.amount,
        ).where(InvoiceFact.tenant_id == t)
    ):
        facts[r[0]].append((str(r[1]), r[2], r[3], r[4], _norm_amount(r[5])))
    out = {}
    for doc in terms.keys() | facts.keys():
        fp = context_fingerprint(terms.get(doc, []), facts.get(doc, []))
        if fp is not None:
            out[doc] = fp
    return out


def _attachments(repo: Repo) -> dict[UUID, set[UUID]]:
    q = select(DocumentLink.document_id, DocumentLink.linked_document_id).where(
        DocumentLink.tenant_id == repo.tenant_id, DocumentLink.link_type == ATTACHMENT_LINK
    )
    out: dict[UUID, set[UUID]] = defaultdict(set)
    for email, att in repo.session.execute(q):
        out[email].add(att)
    return out


def link_duplicates(repo: Repo, store: RawStore) -> dict[UUID, UUID]:
    """Store each document's fingerprint and link its probable duplicates; return duplicate
    id -> original id. Within one fingerprint group, the original is the earliest document
    that isn't a forward, or the earliest forward if all are. A forward links to it; a
    non-forward links to it only if they share an attachment."""
    fps = _fingerprints(repo)
    docs = list(
        repo.session.scalars(
            select(SourceDocument).where(SourceDocument.tenant_id == repo.tenant_id)
        )
    )
    for d in docs:
        if d.context_fingerprint != fps.get(d.id):
            repo.update_document(d.id, context_fingerprint=fps.get(d.id))

    groups: dict[str, list[SourceDocument]] = defaultdict(list)
    for d in docs:
        if d.id in fps:
            groups[fps[d.id]].append(d)
    atts = _attachments(repo)
    far = dt.datetime.max.replace(tzinfo=dt.UTC)

    links: dict[UUID, UUID] = {}
    for fp, group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda d: (d.received_at or far, d.path))
        fwd = {d.id for d in group if is_forward(d.subject, _text(store, d))}
        orig = next((d for d in group if d.id not in fwd), group[0])
        for d in group:
            if d.id == orig.id:
                continue
            if d.id in fwd or atts.get(d.id, set()) & atts.get(orig.id, set()):
                repo.link_documents(d.id, orig.id, PROBABLE_DUPLICATE, fp)
                links[d.id] = orig.id
    repo.prune_links(PROBABLE_DUPLICATE, set(links.items()))
    return links


def _text(store: RawStore, d: SourceDocument) -> str:
    """For the forward check: an email's decoded body text (a base64 or quoted-printable body
    hides its forwarded block in the raw bytes), else the stored text."""
    path = store.path(d.storage_path)
    if is_eml(d.path):
        return parse_eml(path.read_bytes()).body_text
    return read_source_text(path)
