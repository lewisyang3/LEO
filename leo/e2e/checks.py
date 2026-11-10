"""The end-to-end checks (ROADMAP 2.2): a pipeline run's rows against the answer key.

`run_checks` reads one tenant after the worker has drained it and returns one `Check` per item
of the key: the jobs, F1-F5 (each with its draft scored against the key's `expected_action`), no
extra finding, the vendors' names, N1-N8, D1-D4 and, for an LLM drafter, that no draft fell back
to the template (2.3). `check_rerun` is D5: ingest the set again and reconcile again, and no
ledger row may change.

Key vendors are found through the documents the key gives them (a stored document's or term's
vendor), and key charges through their date, descriptor and amount, because every stored id is
the pipeline's own.
"""

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leo.db import models as m
from leo.db.read import load_findings, load_ledger
from leo.eval.scoring import score_draft
from leo.fixtures import ExpectedAction, Fixture
from leo.interfaces import Ledger
from leo.types import ActionDraft, Finding, FindingKind, TermStatus, TermType

PROBABLE_DUPLICATE = "probable_duplicate"
ADDON = "Transactional Email add-on"


@dataclass
class Check:
    id: str
    ok: bool
    detail: str


# --- Shared with tests/test_pipeline.py ------------------------------------------------------


def stuck_jobs(session: Session, tenant_id: UUID) -> list[tuple]:
    """(stage, status, last error) of every job that isn't `done`."""
    q = select(m.Job.stage, m.Job.status, m.Job.last_error).where(
        m.Job.tenant_id == tenant_id, m.Job.status != "done"
    )
    return [tuple(r) for r in session.execute(q)]


def anchor_of(f: Finding, ledger: Ledger) -> tuple:
    """A stored finding's kind and anchor, in terms that don't depend on stored ids: the anchor
    term's type and document, and the anchor charge's date and amount."""
    terms = {t.id: t for t in ledger.terms}
    txns = {t.id: t for t in ledger.transactions}
    t = terms[f.anchor.term]
    x = txns.get(f.anchor.transaction) if f.anchor.transaction else None
    return (str(f.kind), str(t.type), t.document, (x.date, x.amount) if x else None)


def key_anchor_of(f: Finding, fx: Fixture) -> tuple:
    """`anchor_of` for one of the key's findings."""
    t = next(t for t in fx.terms if t.id == f.anchor.term)
    x = next((x for x in fx.transactions if x.id == f.anchor.transaction), None)
    return (str(f.kind), str(t.type), t.document, (x.date, x.amount) if x else None)


# Ledger tables only: a rerun adds `job`, `stage_execution` and `model_usage` rows by design.
SNAPSHOT_MODELS = (
    m.SourceDocument,
    m.Vendor,
    m.VendorAlias,
    m.VendorContact,
    m.ContractTerm,
    m.InvoiceFact,
    m.Transaction,
    m.Finding,
    m.FindingTerm,
    m.FindingTransaction,
    m.FindingInvoiceFact,
    m.Action,
    m.DocumentLink,
    m.Citation,
)


def snapshot(session: Session, tenant_id: UUID) -> dict[str, list]:
    """Every ledger row's id, and each term's and finding's status."""
    out: dict[str, list] = {}
    for model in SNAPSHOT_MODELS:
        out[model.__tablename__] = sorted(
            str(i) for i in session.scalars(select(model.id).where(model.tenant_id == tenant_id))
        )
    for model in (m.ContractTerm, m.Finding):
        out[f"{model.__tablename__}_status"] = sorted(
            (str(i), st)
            for i, st in session.execute(
                select(model.id, model.status).where(model.tenant_id == tenant_id)
            )
        )
    return out


# --- The checks ------------------------------------------------------------------------------


class _Run:
    """One tenant's stored rows, read once, with lookups by the key's names."""

    def __init__(self, session: Session, tenant_id: UUID, fx: Fixture) -> None:
        self.s, self.t, self.fx = session, tenant_id, fx
        self.ledger = load_ledger(session, tenant_id)
        self.findings = load_findings(session, tenant_id, self.ledger)
        self.docs = {
            d.path: d
            for d in session.scalars(
                select(m.SourceDocument).where(m.SourceDocument.tenant_id == tenant_id)
            )
        }
        self.vendor_names = {v.id: v.canonical_name for v in self.ledger.vendors}

    def vendors(self, key_vendor: str) -> set[str]:
        """The stored vendor ids of the key vendor's documents and of their terms."""
        paths = {d.path for d in self.fx.documents if d.vendor == key_vendor}
        out = {d.vendor for d in self.ledger.documents if d.path in paths and d.vendor}
        out |= {t.vendor for t in self.ledger.terms if t.document in paths and t.vendor}
        return out

    def terms(self, key_vendor: str, type: TermType) -> list:
        vs = self.vendors(key_vendor)
        return [t for t in self.ledger.terms if t.vendor in vs and t.type == type]

    def findings_for(self, key_vendor: str, kind: FindingKind | None = None) -> list[Finding]:
        vs = self.vendors(key_vendor)
        return [f for f in self.findings if f.vendor in vs and (kind is None or f.kind == kind)]

    def cited_from(self, path: str) -> int:
        """How many terms, facts and contacts cite the document at `path`."""
        doc = self.docs.get(path)
        if doc is None:
            return 0
        return self.s.scalar(
            select(func.count()).where(
                m.Citation.tenant_id == self.t, m.Citation.document_id == doc.id
            )
        )

    def txn(self, key_txn: str) -> m.Transaction | None:
        x = next(x for x in self.fx.transactions if x.id == key_txn)
        return self.s.scalars(
            select(m.Transaction).where(
                m.Transaction.tenant_id == self.t,
                m.Transaction.date == x.date,
                m.Transaction.descriptor == x.descriptor,
                m.Transaction.amount == x.amount,
            )
        ).one_or_none()

    def describe(self, fs: Iterable[Finding]) -> str:
        return ", ".join(f"{f.kind} ({self.vendor_names.get(f.vendor, f.vendor)})" for f in fs)


def _check(id: str, problems: list[str], ok_detail: str) -> Check:
    return Check(id, not problems, "; ".join(problems) if problems else ok_detail)


def _none(r: _Run, what: str, fs: list[Finding]) -> list[str]:
    return [f"{what}: {r.describe(fs)}"] if fs else []


def check_jobs(r: _Run) -> Check:
    stuck = stuck_jobs(r.s, r.t)
    total = r.s.scalar(select(func.count()).where(m.Job.tenant_id == r.t))
    problems = [f"{stage} {status}: {err}" for stage, status, err in stuck]
    return _check("jobs", problems, f"{total} jobs, all done")


def check_findings(r: _Run) -> list[Check]:
    out = []
    by_anchor = {anchor_of(f, r.ledger): f for f in r.findings}
    for x in r.fx.expected_findings:
        key = x.finding
        f = by_anchor.get(key_anchor_of(key, r.fx))
        if f is None:
            out.append(Check(key.id, False, f"no {key.kind} finding on the key's anchor"))
            continue
        problems = []
        got = (f.deadline, f.recommended_send_by, f.impact.headline_amount)
        want = (key.deadline, key.recommended_send_by, key.impact.headline_amount)
        if got != want:
            problems.append(f"(deadline, send by, impact) {got} != key {want}")
        problems += _draft_problems(r, f, x.expected_action)
        out.append(_check(key.id, problems, f"{f.title} (${f.impact.headline_amount:,})"))
    expected = {key_anchor_of(x.finding, r.fx) for x in r.fx.expected_findings}
    extra = [f for f in r.findings if anchor_of(f, r.ledger) not in expected]
    out.append(
        _check(
            "extra",
            _none(r, "findings the key doesn't have", extra),
            f"{len(r.findings)} current findings, none extra",
        )
    )
    return out


def _actions(r: _Run, finding_id: str) -> list[m.Action]:
    q = select(m.Action).where(m.Action.tenant_id == r.t, m.Action.finding_id == UUID(finding_id))
    return list(r.s.scalars(q.order_by(m.Action.natural_key)))


def _draft_problems(r: _Run, f: Finding, expected: ExpectedAction) -> list[str]:
    """The finding's action of the key's type, scored like the eval's `--drafter`."""
    actions = [a for a in _actions(r, f.id) if a.action_type == str(expected.type)]
    if not actions:
        return [f"no drafted {expected.type}"]
    a = actions[0]
    draft = ActionDraft(
        finding=f.id,
        type=a.action_type,
        to=a.recipient,
        subject=a.subject,
        body=a.body,
        pre_step=a.pre_step,
        alternative=a.alternative,
    )
    s = score_draft(f.id, draft, expected)
    problems = []
    if not s.to_ok:
        problems.append(f"draft to {a.recipient!r}, key {expected.to!r}")
    if not s.alternative_ok:
        problems.append(f"draft alternative {a.alternative}, key {expected.alternative}")
    if not s.pre_step_ok:
        problems.append("draft pre-step " + ("missing" if expected.pre_step else "unexpected"))
    if s.missing:
        problems.append("draft lacks " + ", ".join(repr(x) for x in s.missing))
    return problems


def check_vendors(r: _Run) -> Check:
    """Each key vendor is stored under the key's name: found through its documents, or through
    its charges for a vendor with none."""
    problems = []
    for v in r.fx.vendors:
        ids = r.vendors(v.id)
        if not ids:
            txns = [t for t, kv in r.fx.transaction_vendor_map.items() if kv == v.id]
            ids = {str(x.vendor_id) for t in txns if (x := r.txn(t)) and x.vendor_id}
        names = sorted({r.vendor_names.get(i, i) for i in ids})
        if names != [v.canonical_name]:
            problems.append(f"{v.id}: {names or 'not found'}, key {v.canonical_name!r}")
    return _check("vendors", problems, f"{len(r.fx.vendors)} vendors named as in the key")


def check_drafts(r: _Run) -> Check:
    """Every current finding's actions were written by the LLM, none by the fallback template."""
    problems = []
    for f in r.findings:
        for a in _actions(r, f.id):
            engine = (a.raw_model_output or {}).get("engine", "?")
            if not engine.startswith("llm:"):
                problems.append(f"{r.describe([f])}: {engine}")
    n = sum(len(_actions(r, f.id)) for f in r.findings)
    return _check("drafts", problems, f"{n} drafts, all the LLM's")


def _value(t, name: str):
    return getattr(t.value, name)


def check_n1(r: _Run) -> Check:
    esc = r.terms("ferrolink", TermType.escalator)
    problems = []
    if not any(
        _value(t, "pct") == Decimal(4) and _value(t, "first_application") == dt.date(2026, 8, 1)
        for t in esc
    ):
        problems.append(f"no 4% escalator from 2026-08-01 stored ({len(esc)} escalators)")
    problems += _none(r, "Ferrolink findings", r.findings_for("ferrolink"))
    return _check("N1", problems, "Ferrolink's 4% escalator stored, applied on time, no finding")


def _notice_deadline(r: _Run, key_vendor: str, deadline: dt.date) -> list[str]:
    got = [_value(t, "next_deadline") for t in r.terms(key_vendor, TermType.notice_window)]
    return [] if deadline in got else [f"notice next_deadline {got}, want {deadline}"]


def check_n2(r: _Run) -> Check:
    problems = _notice_deadline(r, "driftwell", dt.date(2027, 6, 1))
    problems += _none(r, "Driftwell findings", r.findings_for("driftwell"))
    return _check("N2", problems, "Driftwell's 2027-06-01 deadline stored, outside lookahead")


def check_n3(r: _Run) -> Check:
    prices = [t for t in r.terms("lumenpost", TermType.price) if t.plan_label == ADDON]
    amounts = sorted({_value(t, "amount") for t in prices})
    problems = [] if amounts == [Decimal(49)] else [f"add-on prices {amounts}, want [49]"]
    problems += _none(r, "add-on findings", [f for f in r.findings if f.plan_label == ADDON])
    return _check("N3", problems, f"add-on stays $49 ({len(prices)} terms), no finding")


def check_n4(r: _Run) -> Check:
    problems = _notice_deadline(r, "kestrel", dt.date(2027, 7, 2))
    fs = r.findings_for("kestrel", FindingKind.auto_renewal_deadline)
    problems += _none(r, "Kestrel renewal findings", fs)
    return _check("N4", problems, "Kestrel's 2027-07-02 deadline stored, no renewal finding")


def check_n5(r: _Run) -> Check:
    path = "inbox/14_zapledger_cold_outreach.eml"
    problems = [f"vendor {n!r}" for n in r.vendor_names.values() if "zap" in n.lower()]
    if n := r.cited_from(path):
        problems.append(f"{n} items cite {path}")
    return _check("N5", problems, "ZapLedger isn't a vendor and gives nothing")


def check_n6(r: _Run) -> Check:
    path = "inbox/15_harbor_supply_receipt.eml"
    problems = []
    if terms := [t for t in r.ledger.terms if t.document == path]:
        problems.append(f"{len(terms)} terms from {path}")
    problems += _none(r, "Harbor findings", r.findings_for("harbor_supply"))
    txn = r.txn("txn_0038")
    vendors = r.vendors("harbor_supply")
    if txn is None:
        problems.append("txn_0038 not stored")
    elif str(txn.vendor_id) not in vendors:
        problems.append(f"txn_0038's vendor {txn.vendor_id} isn't the receipt's {vendors}")
    return _check("N6", problems, "receipt matched to its charge, no terms, no finding")


def check_n7(r: _Run) -> Check:
    seats = [_value(t, "included") for t in r.terms("halcyon", TermType.seat_count)]
    problems = [] if 50 in seats else [f"seat counts {seats}, want 50 included"]
    fs = r.findings_for("halcyon", FindingKind.seats_over_commitment)
    problems += _none(r, "Halcyon seat findings", fs)
    return _check("N7", problems, "Halcyon's 50 included employees stored, no seat finding")


def check_n8(r: _Run) -> Check:
    path = "inbox/11_corvana_event_invite.eml"
    problems = []
    if n := r.cited_from(path):
        problems.append(f"{n} items cite {path}")
    prices = {
        (_value(t, "amount"), str(_value(t, "per"))) for t in r.terms("corvana", TermType.price)
    }
    if prices != {(Decimal(14400), "year")}:
        problems.append(f"Corvana prices {sorted(prices)}, want only 14400/year")
    return _check("N8", problems, "the event invite gives nothing; Corvana stays $14,400/year")


def check_d1(r: _Run) -> Check:
    a, b = "inbox/06_kestrel_invoice_2026_10.eml", "inbox/07_kestrel_invoice_2026_10_resynced.eml"
    problems = []
    if len(r.docs) != len(r.fx.documents) - 1:
        problems.append(f"{len(r.docs)} documents for {len(r.fx.documents)} paths, want one fewer")
    stored = [p for p in (a, b) if p in r.docs]
    if len(stored) != 1:
        problems.append(f"06/07 stored as {stored}, want one row")
    return _check("D1", problems, f"{len(r.docs)} rows; the re-synced copy is the original's row")


def _links(r: _Run) -> list[tuple[str, str]]:
    paths = {d.id: p for p, d in r.docs.items()}
    rows = r.s.execute(
        select(m.DocumentLink.document_id, m.DocumentLink.linked_document_id).where(
            m.DocumentLink.tenant_id == r.t, m.DocumentLink.link_type == PROBABLE_DUPLICATE
        )
    )
    return sorted((paths[a], paths[b]) for a, b in rows)


def check_d2(r: _Run) -> Check:
    want = [
        (
            "inbox/02_fw_lumenpost_price_change_notice.eml",
            "inbox/01_lumenpost_price_change_notice.eml",
        )
    ]
    problems = []
    if (links := _links(r)) != want:
        problems.append(f"duplicate links {links}, want 02 -> 01 only")
    f2 = r.findings_for("lumenpost", FindingKind.price_increase_notice)
    if len(f2) != 1:
        problems.append(f"{len(f2)} price-increase findings for Lumenpost, want 1")
    return _check("D2", problems, "the forward is linked to 01 and counted once")


def check_d3(r: _Run) -> Check:
    a, b = "inbox/05_kestrel_invoice_2026_09.eml", "inbox/06_kestrel_invoice_2026_10.eml"
    problems = []
    if not (a in r.docs and b in r.docs):
        problems.append("05 and 06 aren't both stored")
    if any({x, y} & {a, b} for x, y in _links(r)):
        problems.append("05 or 06 is linked as a duplicate")
    for path, number, period, key_txn in (
        (a, "INV-KCH-260901", "2026-09", "txn_0025"),
        (b, "INV-KCH-261001", "2026-10", "txn_0031"),
    ):
        facts = [f for f in r.ledger.invoice_facts if f.document == path]
        txn = r.txn(key_txn)
        ok = [
            f
            for f in facts
            if f.invoice_number == number
            and f.billing_period == period
            and txn is not None
            and f.matches_transaction == str(txn.id)
        ]
        if len(ok) != 1:
            problems.append(f"{path}: want one {number} ({period}) matched to {key_txn}")
    return _check("D3", problems, "two invoices, two facts, each matched to its own charge")


def check_d4(r: _Run) -> Check:
    prices = r.terms("lumenpost", TermType.price)
    growth = sorted(
        {
            (t.effective_date, _value(t, "amount"), str(t.status))
            for t in prices
            if t.plan_label == "Growth"
        },
        key=lambda x: x[0] or dt.date.min,
    )
    want = [
        (dt.date(2025, 3, 3), Decimal(299), TermStatus.superseded.value),
        (dt.date(2026, 12, 1), Decimal(339), TermStatus.current.value),
    ]
    problems = [] if growth == want else [f"Growth history {growth}, want {want}"]
    addon = {_value(t, "amount") for t in prices if t.plan_label == ADDON}
    if addon != {Decimal(49)}:
        problems.append(f"add-on prices {sorted(addon)}, want only 49")
    return _check("D4", problems, "Growth $299 -> $339 from 2026-12-01; add-on $49 throughout")


CHECKS: tuple[Callable[[_Run], Check], ...] = (
    check_n1,
    check_n2,
    check_n3,
    check_n4,
    check_n5,
    check_n6,
    check_n7,
    check_n8,
    check_d1,
    check_d2,
    check_d3,
    check_d4,
)


def run_checks(
    session: Session, tenant_id: UUID, fx: Fixture, llm_drafts: bool = False
) -> list[Check]:
    """Every check but D5, on one drained tenant. `llm_drafts` adds the `drafts` check, for a
    run whose drafter is the LLM's."""
    r = _Run(session, tenant_id, fx)
    out = [check_jobs(r), *check_findings(r), check_vendors(r), *(c(r) for c in CHECKS)]
    if llm_drafts:
        out.append(check_drafts(r))
    return out


def check_rerun(session: Session, tenant_id: UUID, rerun: Callable[[], int]) -> Check:
    """D5. `rerun` ingests the set again, requests a reconcile, drains, and returns the number of
    new documents. No ledger row may be added, removed or change status."""
    before = snapshot(session, tenant_id)
    new = rerun()
    session.expire_all()
    after = snapshot(session, tenant_id)
    problems = [f"{new} new documents"] if new else []
    for table in before:
        if before[table] != after[table]:
            added = len(set(after[table]) - set(before[table]))
            gone = len(set(before[table]) - set(after[table]))
            problems.append(f"{table}: +{added} -{gone}")
    return _check("D5", problems, "a second ingest and reconcile change no row")


def draft_engines(session: Session, tenant_id: UUID) -> dict[str, str]:
    """Each current finding's title -> its actions' draft engines, for the report."""
    rows = session.execute(
        select(m.Finding.title, m.Action.raw_model_output)
        .join(m.Action, m.Action.finding_id == m.Finding.id)
        .where(m.Finding.tenant_id == tenant_id, m.Finding.status == "current")
        .order_by(m.Finding.title)
    )
    out: dict[str, list[str]] = {}
    for title, raw in rows:
        out.setdefault(title, []).append((raw or {}).get("engine", "?"))
    return {t: ", ".join(e) for t, e in out.items()}
