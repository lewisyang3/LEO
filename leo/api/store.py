"""The fixture-backed store behind the API (ROADMAP C2).

`FixtureStore` serves the answer key's findings (`FixtureReconciler`) with drafts from the
committed LLM cache (`LLMDrafter`, forced offline). Action state and the outbox live in memory,
so a restart resets them. C3 swaps this store for Track A's repository behind the same routes.

Why the key's findings and not `RuleReconciler` (C1): the draft cache key includes the finding
id, and the cache was recorded on the key's ids (F1–F5). C1's `v1:finding:…` ids would miss
every cached draft. Wiring C1 in, with a recache, is Phase 2.1.

"Send" is simulated: it appends the draft to the outbox and marks it `sent`. Nothing leaves.
"""

import datetime
import threading
from functools import cache
from pathlib import Path

from leo.actions.draft import LLMDrafter, TemplateDrafter
from leo.api.schemas import (
    ActionEdit,
    ActionOut,
    Dashboard,
    DocumentOut,
    FindingDetail,
    FindingSummary,
    InvoiceFactOut,
    QuoteRef,
    TermOut,
)
from leo.api.sources import finding_numbers
from leo.config import Settings, get_settings
from leo.fixtures import Fixture, FixtureReconciler, load_fixture
from leo.llm import LLMOfflineMiss, ResponseCache
from leo.rules import locate_quote, read_source_text
from leo.types import ActionDraft, ActionState, Finding

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = "template"


class NotFound(KeyError):
    pass


class InvalidTransition(ValueError):
    pass


def action_id(draft: ActionDraft) -> str:
    return f"{draft.finding}:{draft.type}"


class FixtureStore:
    def __init__(self, fixture: Fixture | None = None, settings: Settings | None = None) -> None:
        self.fixture = fixture or load_fixture()
        self.ledger = self.fixture.ledger()
        self.today = self.fixture.clock().today()
        self.findings: list[Finding] = (
            FixtureReconciler(self.fixture).reconcile(self.ledger, self.today).findings
        )
        self._texts: dict[str, str] = {}
        self._lock = threading.Lock()
        self.outbox: list[ActionOut] = []
        self.actions: dict[str, ActionOut] = {}
        self.draft_records: dict[str, dict] = {}
        self._draft_all(settings or get_settings())

    # --- Building ---------------------------------------------------------------------------

    def _draft_all(self, settings: Settings) -> None:
        cache_dir = settings.leo_llm_cache_dir
        if not cache_dir.is_absolute():
            cache_dir = REPO_ROOT / cache_dir
        sender = self.fixture.customer.name
        llm = LLMDrafter(
            ResponseCache(cache_dir),
            settings=settings.model_copy(update={"leo_llm_offline": True}),
            sender=sender,
        )
        template = TemplateDrafter(sender=sender)
        for f in self.findings:
            try:
                draft = llm.draft(f, self.ledger)
                record = llm.records[f.id]
                engine = TEMPLATE if record["fallback"] else llm.engine
            except LLMOfflineMiss as e:
                draft = template.draft(f, self.ledger)
                record = {"engine": TEMPLATE, "fallback": True, "problems": [str(e)]}
                engine = TEMPLATE
            self.draft_records[f.id] = record
            out = ActionOut(
                id=action_id(draft),
                finding=f.id,
                finding_title=f.title,
                vendor=self.fixture.vendor(f.vendor).canonical_name,
                type=draft.type,
                to=draft.to,
                subject=draft.subject,
                body=draft.body,
                pre_step=draft.pre_step,
                alternative=draft.alternative,
                state=draft.state,
                engine=engine,
                deadline=f.deadline,
                recommended_send_by=f.recommended_send_by,
            )
            self.actions[out.id] = out

    def _text(self, path: str) -> str:
        if path not in self._texts:
            self._texts[path] = read_source_text(self.fixture.root / path)
        return self._texts[path]

    def cite(self, document: str, quote: str) -> QuoteRef:
        span = locate_quote(self._text(document), quote)
        start, end = span if span else (None, None)
        return QuoteRef(document=document, quote=quote, start=start, end=end)

    # --- Reads ------------------------------------------------------------------------------

    def _finding(self, finding_id: str) -> Finding:
        for f in self.findings:
            if f.id == finding_id:
                return f
        raise NotFound(finding_id)

    def _action_for(self, finding_id: str) -> ActionOut:
        return next(a for a in self.actions.values() if a.finding == finding_id)

    def _summary(self, f: Finding) -> FindingSummary:
        due = f.recommended_send_by or f.deadline
        action = self._action_for(f.id)
        return FindingSummary(
            id=f.id,
            kind=f.kind,
            vendor=f.vendor,
            vendor_name=self.fixture.vendor(f.vendor).canonical_name,
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
        out = [self._summary(f) for f in self.findings]
        return sorted(out, key=lambda s: (s.days_left is None, s.days_left or 0, -s.at_stake, s.id))

    def dashboard(self) -> Dashboard:
        return Dashboard(
            today=self.today,
            open_findings=len(self.findings),
            at_stake=sum((f.impact.headline_amount for f in self.findings), start=0),
            pending_actions=sum(
                a.state in (ActionState.draft, ActionState.approved) for a in self.actions.values()
            ),
        )

    def detail(self, finding_id: str) -> FindingDetail:
        f = self._finding(finding_id)
        terms = {t.id: t for t in self.ledger.terms}
        facts = {x.id: x for x in self.ledger.invoice_facts}
        txns = {t.id: t for t in self.ledger.transactions}
        return FindingDetail(
            summary=self._summary(f),
            finding=f,
            vendor=self.fixture.vendor(f.vendor),
            numbers=finding_numbers(f, self.ledger),
            terms=[
                TermOut(
                    term=terms[t],
                    citations=[self.cite(terms[t].document, q) for q in terms[t].quotes],
                )
                for t in f.terms
            ],
            invoice_facts=[
                InvoiceFactOut(
                    fact=facts[x],
                    citations=[self.cite(facts[x].document, q) for q in facts[x].quotes],
                )
                for x in f.invoice_facts
            ],
            transactions=[txns[t] for t in f.transactions],
            documents=[self.fixture.document(d) for d in f.documents],
            linked_duplicates=f.linked_duplicates,
            action=self._action_for(f.id),
        )

    def document(self, path: str) -> DocumentOut:
        doc = next((d for d in self.fixture.documents if d.path == path), None)
        if doc is None:
            raise NotFound(path)
        return DocumentOut(
            path=doc.path, doc_class=doc.doc_class, vendor=doc.vendor, text=self._text(doc.path)
        )

    def queue(self) -> list[ActionOut]:
        return [self.actions[s.action_id] for s in self.summaries()]

    # --- Action transitions -----------------------------------------------------------------

    def _action(self, action_id: str) -> ActionOut:
        if action_id not in self.actions:
            raise NotFound(action_id)
        return self.actions[action_id]

    def _move(self, action_id: str, frm: ActionState, to: ActionState) -> ActionOut:
        with self._lock:
            a = self._action(action_id)
            if a.state != frm:
                verb = "restore" if to == ActionState.draft else to.value
                raise InvalidTransition(f"can't {verb} an action that is {a.state.value}")
            a.state = to
            if to == ActionState.sent:
                a.sent_at = datetime.datetime.now(datetime.UTC)
                self.outbox.append(a)
            return a

    def edit(self, action_id: str, edit: ActionEdit) -> ActionOut:
        with self._lock:
            a = self._action(action_id)
            if a.state != ActionState.draft:
                raise InvalidTransition(f"can't edit an action that is {a.state.value}")
            a.to, a.subject, a.body = edit.to, edit.subject, edit.body
            a.edited = True
            return a

    def approve(self, action_id: str) -> ActionOut:
        return self._move(action_id, ActionState.draft, ActionState.approved)

    def reject(self, action_id: str) -> ActionOut:
        return self._move(action_id, ActionState.draft, ActionState.rejected)

    def restore(self, action_id: str) -> ActionOut:
        """Undo a reject."""
        return self._move(action_id, ActionState.rejected, ActionState.draft)

    def send(self, action_id: str) -> ActionOut:
        return self._move(action_id, ActionState.approved, ActionState.sent)


@cache
def get_store() -> FixtureStore:
    return FixtureStore()
