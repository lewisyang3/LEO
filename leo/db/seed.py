"""Load the synthetic answer key into the database through the repository (ROADMAP A2).

Vendors, transaction mappings and duplicate links come straight from the key, the way the
fixture stages in `leo.fixtures` serve them. A5 and A6 replace those with matching and
detection. Every write is an upsert on a natural key, so seeding twice changes nothing.
Documents go through `leo.ingest.ingest` (A3), which stores real raw copies. A7 wires this
to `make seed` and `make reset`.

Fixture ids (`lp_growth_price_v1`, `F2`, `txn_0001`) live only in this module's maps; the
database keys rows on natural keys and UUIDs.
"""

from typing import Any
from uuid import UUID

from pydantic_core import to_jsonable_python
from sqlalchemy.orm import Session

from leo.config import get_settings
from leo.db import keys
from leo.db.repo import Repo, upsert_tenant
from leo.fixtures import Fixture, FixtureDrafter, load_fixture
from leo.ingest.ingest import ingest
from leo.ingest.store import RawStore
from leo.rules import read_source_text, span_key

TXN_SOURCE = "csv"
PROBABLE_DUPLICATE = "probable_duplicate"


def _jsonb(value: Any) -> Any:
    """JSON-ready values for JSONB: the engine's json.dumps can't take Decimal, date or models."""
    return None if value is None else to_jsonable_python(value)


class _Seeder:
    def __init__(
        self, session: Session, fixture: Fixture, tenant_id: UUID, store: RawStore
    ) -> None:
        self.fx = fixture
        self.repo = Repo(session, tenant_id)
        self.store = store
        self.vendors: dict[str, UUID] = {}
        self.docs: dict[str, UUID] = {}  # path -> id (D1's two paths share one id)
        self.doc_keys: dict[str, str] = {}
        self.texts: dict[str, str] = {}
        self.txns: dict[str, UUID] = {}
        self.txn_keys: dict[str, str] = {}
        self.terms: dict[str, UUID] = {}
        self.term_keys: dict[str, str] = {}
        self.facts: dict[str, UUID] = {}

    def _vendor(self, vendor: str | None) -> UUID | None:
        return self.vendors[vendor] if vendor else None

    def run(self) -> None:
        self.seed_vendors()
        self.seed_documents()
        self.seed_transactions()
        self.seed_terms()
        self.seed_invoice_facts()
        self.seed_contacts()
        self.seed_findings()

    def seed_vendors(self) -> None:
        for v in self.fx.vendors:
            vid = self.repo.upsert_vendor(v.canonical_name, notes=v.notes)
            self.vendors[v.id] = vid
            for alias in v.aliases:
                self.repo.add_alias(vid, alias)

    def seed_documents(self) -> None:
        """Every document goes through `ingest`, which stores the raw copy. The key's own
        columns (class, vendor, notes) are set once per row: a byte-identical copy (D1)
        reuses the original's row and doesn't overwrite them."""
        seen: set[UUID] = set()
        for d in self.fx.documents:
            path = self.fx.root / d.path
            r = ingest(self.repo, self.store, path.read_bytes(), path.name, path=d.path)
            self.doc_keys[d.path] = r.natural_key
            self.docs[d.path] = r.document_id
            self.texts[d.path] = read_source_text(path)
            if r.document_id in seen:
                continue
            seen.add(r.document_id)
            self.repo.update_document(
                r.document_id,
                doc_class=str(d.doc_class) if d.doc_class else None,
                vendor_id=self._vendor(d.vendor),
                notes=d.notes,
            )
        for d in self.fx.documents:
            if d.probable_duplicate_of:
                self.repo.link_documents(
                    self.docs[d.path], self.docs[d.probable_duplicate_of], PROBABLE_DUPLICATE
                )

    def seed_transactions(self) -> None:
        for t in self.fx.transactions:
            key = keys.txn(TXN_SOURCE, t.id)
            self.txn_keys[t.id] = key
            self.txns[t.id] = self.repo.upsert_transaction(
                key,
                vendor_id=self._vendor(self.fx.transaction_vendor_map.get(t.id)),
                source=TXN_SOURCE,
                external_id=t.id,
                descriptor=t.descriptor,
                card_last4=t.card_last4,
                seats_billed=t.seats_billed,
                amount=t.amount,
                currency=t.currency,
                date=t.date,
                plan_label=self.fx.transaction_plan_map.get(t.id),
            )

    def seed_terms(self) -> None:
        for t in self.fx.terms:
            text = self.texts[t.document]
            key = keys.term(self.doc_keys[t.document], str(t.type), span_key(text, t.quotes[0]))
            self.term_keys[t.id] = key
            tid = self.repo.upsert_term(
                key,
                vendor_id=self.vendors[t.vendor],
                document_id=self.docs[t.document],
                type=str(t.type),
                value=_jsonb(t.value),
                old_value=_jsonb(t.old_value),
                plan_label=t.plan_label,
                agreement_ref=t.agreement_ref,
                effective_date=t.effective_date,
                status=str(t.status),
                notes=t.notes,
            )
            self.terms[t.id] = tid
            self.repo.set_citations("term_id", tid, self.docs[t.document], text, t.quotes)
        for t in self.fx.terms:
            self.repo.set_supersession(
                self.terms[t.id],
                self.terms[t.superseded_by] if t.superseded_by else None,
                self.terms[t.supersedes] if t.supersedes else None,
            )

    def seed_invoice_facts(self) -> None:
        for f in self.fx.invoice_facts:
            text = self.texts[f.document]
            key = keys.invoice_fact(self.doc_keys[f.document], span_key(text, f.quotes[0]))
            fid = self.repo.upsert_invoice_fact(
                key,
                vendor_id=self.vendors[f.vendor],
                document_id=self.docs[f.document],
                billing_period=f.billing_period,
                invoice_date=f.invoice_date,
                amount=f.amount,
                invoice_number=f.invoice_number,
                seats_billed=f.seats_billed,
                matches_transaction=self.txns[f.matches_transaction]
                if f.matches_transaction
                else None,
                notes=f.notes,
                status=str(f.status),
            )
            self.facts[f.id] = fid
            self.repo.set_citations("invoice_fact_id", fid, self.docs[f.document], text, f.quotes)

    def seed_contacts(self) -> None:
        for v in self.fx.vendors:
            for c in v.contacts:
                cid = self.repo.upsert_contact(self.vendors[v.id], c.email, c.purpose)
                doc = self.docs[c.document]
                self.repo.set_citations(
                    "vendor_contact_id", cid, doc, self.texts[c.document], [c.quote]
                )

    def seed_findings(self) -> None:
        drafter = FixtureDrafter(self.fx)
        ledger = self.fx.ledger()
        for f in self.fx.findings:
            anchor_txn = f.anchor.transaction
            key = keys.finding(
                self.term_keys[f.anchor.term],
                self.txn_keys[anchor_txn] if anchor_txn else None,
                str(f.kind),
            )
            fid = self.repo.upsert_finding(
                key,
                vendor_id=self.vendors[f.vendor],
                kind=str(f.kind),
                title=f.title,
                plan_label=f.plan_label,
                computation=_jsonb(getattr(f, "computation", None)),
                anchor_term_id=self.terms[f.anchor.term],
                anchor_transaction_id=self.txns[anchor_txn] if anchor_txn else None,
                deadline=f.deadline,
                recommended_send_by=f.recommended_send_by,
                deadline_notes=f.deadline_notes,
                impact=_jsonb(f.impact),
            )
            basis = f.deadline_basis
            for term in f.terms:
                is_basis = basis is not None and term in basis.terms
                self.repo.link_finding_term(fid, self.terms[term], is_basis)
            for txn in f.transactions:
                self.repo.link_finding_transaction(fid, self.txns[txn])
            for fact in f.invoice_facts:
                is_basis = basis is not None and basis.invoice_fact == fact
                self.repo.link_finding_invoice_fact(fid, self.facts[fact], is_basis)

            draft = drafter.draft(f, ledger)
            self.repo.upsert_action(
                keys.action(key, str(draft.type)),
                finding_id=fid,
                action_type=str(draft.type),
                recipient=draft.to,
                subject=draft.subject,
                body=draft.body,
                pre_step=draft.pre_step,
                alternative=str(draft.alternative) if draft.alternative else None,
            )


def seed(
    session: Session,
    fixture: Fixture | None = None,
    slug: str | None = None,
    store: RawStore | None = None,
) -> UUID:
    """Upsert the whole answer key under one tenant and return its id. Doesn't commit.
    Raw copies go to `store`, by default the `LEO_RAW_DIR` store."""
    fixture = fixture or load_fixture()
    store = store or RawStore(get_settings().leo_raw_dir)
    tenant_id = upsert_tenant(session, slug or fixture.dataset, fixture.customer.name)
    _Seeder(session, fixture, tenant_id, store).run()
    return tenant_id
