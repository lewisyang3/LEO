"""The Postgres-backed store behind the API (ROADMAP C3).

`DbStore` serves one tenant's findings, documents and actions from the database, through
`leo.db.read`. It serves whatever findings and actions are stored: the seed's answer-key rows
until the pipeline (2.1) writes its own. A draft's `engine` is `raw_model_output["engine"]`,
or `fixture` for the seed's drafts.

Action decisions are rows, so they survive a restart and a reseed (`Repo.upsert_action` never
overwrites a decided or edited draft). Each transition is one conditional UPDATE, so two
clicks can't both win. "Send" is simulated: the action is marked `sent` with `sent_at`, and
the outbox is the sent actions. Nothing leaves LEO.
"""

import datetime
import hashlib
from collections import Counter
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from leo.api.export import findings_csv, terms_csv
from leo.api.schemas import (
    ActionEdit,
    ActionOut,
    Dashboard,
    DocumentOut,
    FindingDetail,
    FindingSummary,
    InvoiceFactOut,
    JobCount,
    MatchedTransaction,
    PricePoint,
    PriceSeries,
    QuoteRef,
    Stats,
    TermOut,
    UploadOut,
    VendorDetail,
    VendorSummary,
)
from leo.api.sources import finding_numbers
from leo.db import models as m
from leo.db.read import (
    StoredCitation,
    load_citations,
    load_findings,
    load_ledger,
    load_price_history,
)
from leo.db.repo import Repo
from leo.ingest.eml import safe_filename
from leo.ingest.ingest import UPLOADS, ingest
from leo.ingest.store import RawStore
from leo.interfaces import Ledger
from leo.rules import read_source_text
from leo.types import ActionState, ExtractedTerm, Finding, PriceTerm, TermStatus

FIXTURE_ENGINE = "fixture"


class NotFound(KeyError):
    pass


class InvalidTransition(ValueError):
    pass


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound(value) from None


def _quote(c: StoredCitation) -> QuoteRef:
    return QuoteRef(document=c.document, quote=c.quote, start=c.start, end=c.end)


class DbStore:
    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        today: datetime.date,
        raw: RawStore,
        approver: str,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.today = today
        self.raw = raw
        self.approver = approver

    # --- Loading ----------------------------------------------------------------------------

    def _ledger(self) -> Ledger:
        return load_ledger(self.session, self.tenant_id)

    def _findings(self, ledger: Ledger) -> list[Finding]:
        return load_findings(self.session, self.tenant_id, ledger)

    def _action_rows(self) -> list[m.Action]:
        q = select(m.Action).where(m.Action.tenant_id == self.tenant_id).order_by(m.Action.id)
        return list(self.session.scalars(q))

    def _action_out(self, a: m.Action, f: Finding, vendor_name: str) -> ActionOut:
        raw = a.raw_model_output or {}
        return ActionOut(
            id=str(a.id),
            finding=f.id,
            finding_title=f.title,
            vendor=vendor_name,
            type=a.action_type,
            to=a.recipient,
            subject=a.subject,
            body=a.body,
            pre_step=a.pre_step,
            alternative=a.alternative,
            state=a.state,
            engine=raw.get("engine") or FIXTURE_ENGINE,
            edited=a.edited_at is not None,
            sent_at=a.sent_at,
            deadline=f.deadline,
            recommended_send_by=f.recommended_send_by,
        )

    def _view(self) -> tuple[Ledger, list[Finding], dict[str, ActionOut]]:
        """The ledger, current findings, and each finding's action (finding id -> action)."""
        ledger = self._ledger()
        findings = self._findings(ledger)
        names = {v.id: v.canonical_name for v in ledger.vendors}
        by_id = {f.id: f for f in findings}
        actions: dict[str, ActionOut] = {}
        for a in self._action_rows():
            f = by_id.get(str(a.finding_id))
            if f is not None and f.id not in actions:
                actions[f.id] = self._action_out(a, f, names[f.vendor])
        return ledger, [f for f in findings if f.id in actions], actions

    # --- Reads ------------------------------------------------------------------------------

    def _summary(self, f: Finding, action: ActionOut, vendor_name: str) -> FindingSummary:
        due = f.recommended_send_by or f.deadline
        return FindingSummary(
            id=f.id,
            kind=f.kind,
            vendor=f.vendor,
            vendor_name=vendor_name,
            title=f.title,
            plan_label=f.plan_label,
            deadline=f.deadline,
            recommended_send_by=f.recommended_send_by,
            days_left=(due - self.today).days if due else None,
            at_stake=f.impact.headline_amount,
            action_id=action.id,
            action_state=action.state,
        )

    def summaries(self) -> list[FindingSummary]:
        _, findings, actions = self._view()
        return self._sorted_summaries(findings, actions)

    def _sorted_summaries(
        self, findings: list[Finding], actions: dict[str, ActionOut]
    ) -> list[FindingSummary]:
        out = [self._summary(f, actions[f.id], actions[f.id].vendor) for f in findings]
        return sorted(out, key=lambda s: (s.days_left is None, s.days_left or 0, -s.at_stake, s.id))

    def dashboard(self) -> Dashboard:
        _, findings, actions = self._view()
        return Dashboard(
            today=self.today,
            open_findings=len(findings),
            at_stake=sum((f.impact.headline_amount for f in findings), start=0),
            pending_actions=sum(
                a.state in (ActionState.draft, ActionState.approved) for a in actions.values()
            ),
        )

    def detail(self, finding_id: str) -> FindingDetail:
        _uuid(finding_id)
        ledger, findings, actions = self._view()
        f = next((x for x in findings if x.id == finding_id), None)
        if f is None:
            raise NotFound(finding_id)
        cites = load_citations(self.session, self.tenant_id)
        terms = {t.id: t for t in ledger.terms}
        facts = {x.id: x for x in ledger.invoice_facts}
        txns = {t.id: t for t in ledger.transactions}
        docs = {d.path: d for d in ledger.documents}
        vendor = next(v for v in ledger.vendors if v.id == f.vendor)
        action = actions[f.id]
        return FindingDetail(
            summary=self._summary(f, action, vendor.canonical_name),
            finding=f,
            vendor=vendor,
            numbers=finding_numbers(f, ledger),
            terms=[
                TermOut(term=terms[t], citations=[_quote(c) for c in cites.get(t, [])])
                for t in f.terms
            ],
            invoice_facts=[
                InvoiceFactOut(fact=facts[x], citations=[_quote(c) for c in cites.get(x, [])])
                for x in f.invoice_facts
            ],
            transactions=[txns[t] for t in f.transactions],
            documents=[docs[d] for d in f.documents],
            linked_duplicates=f.linked_duplicates,
            action=action,
        )

    def document(self, path: str) -> DocumentOut:
        """Looked up by row, so a path outside the tenant's documents is just not found."""
        row = self.session.scalars(
            select(m.SourceDocument)
            .where(m.SourceDocument.tenant_id == self.tenant_id, m.SourceDocument.path == path)
            .order_by(m.SourceDocument.id)
        ).first()
        if row is None:
            raise NotFound(path)
        return DocumentOut(
            path=row.path,
            doc_class=row.doc_class,
            vendor=None if row.vendor_id is None else str(row.vendor_id),
            text=read_source_text(self.raw.path(row.storage_path)),
        )

    def queue(self) -> list[ActionOut]:
        _, _, actions = self._view()
        return [actions[s.id] for s in self.summaries()]

    def outbox(self) -> list[ActionOut]:
        _, _, actions = self._view()
        sent = [a for a in actions.values() if a.state == ActionState.sent]
        never = datetime.datetime.min.replace(tzinfo=datetime.UTC)
        return sorted(sent, key=lambda a: (a.sent_at or never, a.id))

    def _one_action(self, action_id: str) -> ActionOut:
        _, _, actions = self._view()
        a = next((x for x in actions.values() if x.id == action_id), None)
        if a is None:
            raise NotFound(action_id)
        return a

    # --- Vendors (C5) -----------------------------------------------------------------------

    @staticmethod
    def _duplicate_paths(ledger: Ledger) -> set[str]:
        """Documents linked as a probable duplicate of another: their facts count once, under
        the original."""
        return {d.path for d in ledger.documents if d.probable_duplicate_of}

    @staticmethod
    def _vendor_terms(ledger: Ledger, vendor_id: str, dups: set[str]) -> list[ExtractedTerm]:
        return [t for t in ledger.terms if t.vendor == vendor_id and t.document not in dups]

    @staticmethod
    def _vendor_transactions(ledger: Ledger, vendor_id: str):
        return [
            t for t in ledger.transactions if ledger.transaction_vendor_map.get(t.id) == vendor_id
        ]

    def vendors(self) -> list[VendorSummary]:
        ledger, findings, _ = self._view()
        dups = self._duplicate_paths(ledger)
        out = []
        for v in ledger.vendors:
            mine = [f for f in findings if f.vendor == v.id]
            out.append(
                VendorSummary(
                    id=v.id,
                    canonical_name=v.canonical_name,
                    aliases=v.aliases,
                    terms=len(self._vendor_terms(ledger, v.id, dups)),
                    transactions=len(self._vendor_transactions(ledger, v.id)),
                    open_findings=len(mine),
                    at_stake=sum((f.impact.headline_amount for f in mine), start=0),
                )
            )
        return out

    def vendor(self, vendor_id: str) -> VendorDetail:
        vid = _uuid(vendor_id)
        ledger, findings, actions = self._view()
        vendor = next((v for v in ledger.vendors if v.id == vendor_id), None)
        if vendor is None:
            raise NotFound(vendor_id)
        cites = load_citations(self.session, self.tenant_id)
        dups = self._duplicate_paths(ledger)
        terms = {t.id: t for t in ledger.terms}

        def term_out(t: ExtractedTerm) -> TermOut:
            return TermOut(term=t, citations=[_quote(c) for c in cites.get(t.id, [])])

        history = []
        for series in load_price_history(self.session, self.tenant_id, vid):
            points: list[PricePoint] = []
            prev: PriceTerm | None = None
            for tid in series.terms:
                t = terms[tid]
                assert isinstance(t, PriceTerm)
                change = None
                if (
                    prev is not None
                    and prev.value.amount is not None
                    and t.value.amount is not None
                    and (prev.value.currency, prev.value.per) == (t.value.currency, t.value.per)
                ):
                    change = t.value.amount - prev.value.amount
                points.append(
                    PricePoint(
                        term=term_out(t),
                        in_effect=t.effective_date is None or t.effective_date <= self.today,
                        change=change,
                    )
                )
                prev = t
            history.append(
                PriceSeries(
                    agreement_ref=series.agreement_ref,
                    plan_label=series.plan_label,
                    points=points,
                )
            )

        mine = [f for f in findings if f.vendor == vendor_id]
        invoice_for = {
            f.matches_transaction: f
            for f in ledger.invoice_facts
            if f.matches_transaction
            and f.document not in dups
            and f.status != TermStatus.needs_review
        }
        transactions = [
            MatchedTransaction(
                transaction=t,
                plan_label=ledger.transaction_plan_map.get(t.id),
                invoice_fact=invoice_for.get(t.id),
                findings=[f.id for f in mine if t.id in f.transactions],
            )
            for t in self._vendor_transactions(ledger, vendor_id)
        ]
        return VendorDetail(
            vendor=vendor,
            findings=self._sorted_summaries(mine, actions),
            price_history=history,
            terms=[term_out(t) for t in self._vendor_terms(ledger, vendor_id, dups)],
            transactions=transactions,
            linked_duplicates=sorted(
                d.path
                for d in ledger.documents
                if d.vendor == vendor_id and d.probable_duplicate_of
            ),
        )

    # --- Action transitions -----------------------------------------------------------------

    def _update(self, action_id: str, frm: ActionState, verb: str, **values) -> ActionOut:
        """One conditional UPDATE: it applies only if the action is still in `frm`."""
        aid = _uuid(action_id)
        done = self.session.scalar(
            update(m.Action)
            .where(
                m.Action.id == aid,
                m.Action.tenant_id == self.tenant_id,
                m.Action.state == frm,
            )
            .values(**values)
            .returning(m.Action.id)
        )
        if done is None:
            state = self.session.scalar(
                select(m.Action.state).where(
                    m.Action.id == aid, m.Action.tenant_id == self.tenant_id
                )
            )
            if state is None:
                raise NotFound(action_id)
            raise InvalidTransition(f"can't {verb} an action that is {state}")
        self.session.commit()
        return self._one_action(action_id)

    def edit(self, action_id: str, edit: ActionEdit) -> ActionOut:
        return self._update(
            action_id,
            ActionState.draft,
            "edit",
            recipient=edit.to,
            subject=edit.subject,
            body=edit.body,
            edited_at=func.now(),
        )

    def approve(self, action_id: str) -> ActionOut:
        return self._update(
            action_id,
            ActionState.draft,
            "approve",
            state=ActionState.approved,
            approver=self.approver,
            decided_at=func.now(),
        )

    def reject(self, action_id: str) -> ActionOut:
        return self._update(
            action_id,
            ActionState.draft,
            "reject",
            state=ActionState.rejected,
            decided_at=func.now(),
        )

    def restore(self, action_id: str) -> ActionOut:
        """Undo a reject."""
        return self._update(
            action_id,
            ActionState.rejected,
            "restore",
            state=ActionState.draft,
            approver=None,
            decided_at=None,
        )

    def send(self, action_id: str) -> ActionOut:
        return self._update(
            action_id, ActionState.approved, "send", state=ActionState.sent, sent_at=func.now()
        )

    # --- Export (C4) ------------------------------------------------------------------------

    def export_terms(self) -> str:
        """The term record as CSV: every stored term, in every status."""
        return terms_csv(self._ledger())

    def export_findings(self) -> str:
        """The findings as CSV, in the dashboard's order."""
        _, findings, actions = self._view()
        return findings_csv(self._sorted_summaries(findings, actions), findings, actions)

    # --- Upload and stats -------------------------------------------------------------------

    def upload(self, filename: str, raw: bytes) -> UploadOut:
        """Ingest one file (A3), queue it for the worker (A4) and commit.

        The path is `uploads/{sha8}-{name}`, so two different files with one name don't share
        a path. Bytes already ingested keep their row and its first path."""
        name = safe_filename(filename)
        if name is None:
            raise ValueError(f"not a usable filename: {filename!r}")
        sha8 = hashlib.sha256(raw).hexdigest()[:8]
        try:
            r = ingest(
                Repo(self.session, self.tenant_id),
                self.raw,
                raw,
                name,
                path=f"{UPLOADS}/{sha8}-{name}",
            )
            path = self.session.scalar(
                select(m.SourceDocument.path).where(m.SourceDocument.id == r.document_id)
            )
            self.session.commit()
        except BaseException:
            self.session.rollback()
            raise
        return UploadOut(
            document_id=str(r.document_id),
            path=path,
            created=r.created,
            attachments=[str(a) for a in r.attachment_ids],
        )

    def _count(self, *cols) -> list[tuple]:
        model = cols[0].class_
        q = select(*cols, func.count()).where(model.tenant_id == self.tenant_id).group_by(*cols)
        return [tuple(row) for row in self.session.execute(q).all()]

    def stats(self) -> Stats:
        docs = Counter({k or "unclassified": n for k, n in self._count(m.SourceDocument.doc_class)})
        findings = dict(self._count(m.Finding.status)).get("current", 0)
        jobs = self._count(m.Job.stage, m.Job.processed_by, m.Job.status)
        return Stats(
            documents=sum(docs.values()),
            documents_by_class=dict(sorted(docs.items())),
            terms_by_status=dict(sorted(self._count(m.ContractTerm.status))),
            invoice_facts_by_status=dict(sorted(self._count(m.InvoiceFact.status))),
            findings=findings,
            actions_by_state=dict(sorted(self._count(m.Action.state))),
            jobs=[
                JobCount(stage=s, processed_by=p, status=st, count=n)
                for s, p, st, n in sorted(jobs, key=lambda j: (j[0], j[1] or "", j[2]))
            ],
        )
