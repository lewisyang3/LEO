"""Load the synthetic answer key into the database through the repository (ROADMAP A2).

Terms, invoice facts, contacts and findings come from the key, the way the fixture stages in
`leo.fixtures` serve them. Vendors and matches don't (A5): each document's vendor name, as
`FixtureExtractor` reports it, goes through `DbVendorResolver`; transactions come from
`transactions.csv` and resolve by descriptor; plans and invoice-to-charge links are matched
from the stored terms. A forward (D2) gets its own copy of its original's terms and invoice
facts, cited in its own text, as an extractor would give it; `link_duplicates` (A6) then links
it from the stored items, never from the key. Every write is an upsert on a natural key, so
seeding twice changes nothing. Documents go through
`leo.ingest.ingest` (A3), which stores real raw copies. `make seed` runs `main()` (C3);
`make reset` (A7) wipes the schema and reseeds.

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
from leo.match.duplicates import link_duplicates
from leo.match.transactions import (
    SOURCE as TXN_SOURCE,
)
from leo.match.transactions import (
    CsvTransactionSource,
    match_invoice_facts,
    match_plans,
    sync_transactions,
)
from leo.match.vendors import DbVendorResolver
from leo.rules import read_source_text, span_key
from leo.types import DocumentClass, ExtractedTerm, InvoiceFact


def _jsonb(value: Any) -> Any:
    """JSON-ready values for JSONB: the engine's json.dumps can't take Decimal, date or models."""
    return None if value is None else to_jsonable_python(value)


class _Seeder:
    def __init__(
        self, session: Session, fixture: Fixture, tenant_id: UUID, store: RawStore
    ) -> None:
        self.fx = fixture
        self.repo = Repo(session, tenant_id)
        self.resolver = DbVendorResolver(self.repo)
        self.store = store
        self.vendors: dict[str, UUID] = {}  # key vendor id -> resolved vendor id
        self.docs: dict[str, UUID] = {}  # path -> id (D1's two paths share one id)
        self.doc_keys: dict[str, str] = {}
        self.texts: dict[str, str] = {}
        self.txns: dict[str, UUID] = {}
        self.txn_keys: dict[str, str] = {}
        self.terms: dict[str, UUID] = {}
        self.term_keys: dict[str, str] = {}
        self.facts: dict[str, UUID] = {}

    def run(self) -> None:
        self.seed_documents()
        self.seed_transactions()
        self.seed_terms()
        self.seed_invoice_facts()
        self.seed_forward_copies()
        match_plans(self.repo)
        match_invoice_facts(self.repo)
        link_duplicates(self.repo, self.store)
        self.seed_contacts()
        self.seed_findings()

    def _resolve(self, key_vendor: str, create: bool) -> UUID | None:
        """The document's vendor name as extracted. Noise only looks up, never creates."""
        name = self.fx.vendor(key_vendor).canonical_name
        vid = self.resolver.vendor_for_name(name) if create else self.resolver.find_vendor(name)
        if vid is None:
            return None
        resolved = UUID(vid)
        if self.vendors.setdefault(key_vendor, resolved) != resolved:
            raise RuntimeError(f"{key_vendor!r} resolved to two vendors")
        return resolved

    def seed_documents(self) -> None:
        """Every document goes through `ingest`, which stores the raw copy. The key's own
        columns (class, notes) and the resolved vendor are set once per row: a
        byte-identical copy (D1) reuses the original's row and doesn't overwrite them.
        Non-noise documents resolve first, so noise finds their vendors without creating any
        (ZapLedger never becomes one)."""
        for d in self.fx.documents:
            path = self.fx.root / d.path
            r = ingest(
                self.repo, self.store, path.read_bytes(), path.name, path=d.path, enqueue=False
            )
            self.doc_keys[d.path] = r.natural_key
            self.docs[d.path] = r.document_id
            self.texts[d.path] = read_source_text(path)
        seen: set[UUID] = set()
        for d in sorted(self.fx.documents, key=lambda d: d.doc_class == DocumentClass.noise):
            doc_id = self.docs[d.path]
            if doc_id in seen:
                continue
            seen.add(doc_id)
            is_noise = d.doc_class == DocumentClass.noise
            self.repo.update_document(
                doc_id,
                doc_class=str(d.doc_class) if d.doc_class else None,
                vendor_id=self._resolve(d.vendor, create=not is_noise) if d.vendor else None,
                notes=d.notes,
            )

    def seed_transactions(self) -> None:
        source = CsvTransactionSource(self.fx.root / "transactions.csv")
        self.txns = sync_transactions(self.repo, source, self.resolver)
        self.txn_keys = {ext: keys.txn(TXN_SOURCE, ext) for ext in self.txns}

    def seed_terms(self) -> None:
        for t in self.fx.terms:
            self.term_keys[t.id], self.terms[t.id] = self._term(t, t.document)
        for t in self.fx.terms:
            self.repo.set_supersession(
                self.terms[t.id],
                self.terms[t.superseded_by] if t.superseded_by else None,
                self.terms[t.supersedes] if t.supersedes else None,
            )

    def _term(self, t: ExtractedTerm, path: str) -> tuple[str, UUID]:
        """Upsert the key's term as found in document `path` (its own, or a forward's)."""
        text = self.texts[path]
        key = keys.term(self.doc_keys[path], str(t.type), span_key(text, t.quotes[0]))
        tid = self.repo.upsert_term(
            key,
            vendor_id=self.vendors[t.vendor],
            document_id=self.docs[path],
            type=str(t.type),
            value=_jsonb(t.value),
            old_value=_jsonb(t.old_value),
            plan_label=t.plan_label,
            agreement_ref=t.agreement_ref,
            effective_date=t.effective_date,
            status=str(t.status),
            notes=t.notes,
        )
        self.repo.set_citations("term_id", tid, self.docs[path], text, t.quotes)
        return key, tid

    def seed_invoice_facts(self) -> None:
        for f in self.fx.invoice_facts:
            self.facts[f.id] = self._invoice_fact(f, f.document)

    def _invoice_fact(self, f: InvoiceFact, path: str) -> UUID:
        text = self.texts[path]
        key = keys.invoice_fact(self.doc_keys[path], span_key(text, f.quotes[0]))
        fid = self.repo.upsert_invoice_fact(
            key,
            vendor_id=self.vendors[f.vendor],
            document_id=self.docs[path],
            billing_period=f.billing_period,
            invoice_date=f.invoice_date,
            amount=f.amount,
            invoice_number=f.invoice_number,
            seats_billed=f.seats_billed,
            notes=f.notes,
            status=str(f.status),
        )
        self.repo.set_citations("invoice_fact_id", fid, self.docs[path], text, f.quotes)
        return fid

    def seed_forward_copies(self) -> None:
        """A forward (02) holds its original's items in its own text. Store them on the
        forward, as an extractor would, with no supersession and no findings: the key's
        findings anchor on the original. Contacts aren't copied (their key would collide)."""
        for d in self.fx.documents:
            if d.probable_duplicate_of:
                for t in self.fx.terms_for(d.probable_duplicate_of):
                    self._term(t, d.path)
                for f in self.fx.invoice_facts_for(d.probable_duplicate_of):
                    self._invoice_fact(f, d.path)

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


def main() -> None:
    """`make seed`: load the answer key into the configured database and commit."""
    from leo.db.session import get_sessionmaker

    with get_sessionmaker()() as session:
        tenant_id = seed(session, slug=get_settings().leo_tenant)
        session.commit()
    print(f"seeded tenant {tenant_id}")


if __name__ == "__main__":
    main()
