"""Transactions, vendors and matching (A5). The answer key is used only to check results.

The database tests need Postgres running and migrated (`make up && make migrate`), and run in
the rolled-back `session` fixture from conftest.py.
"""

from collections import defaultdict
from uuid import UUID

import pytest
from sqlalchemy import select

from leo.db import models as m
from leo.db.models import new_uuid7
from leo.db.repo import Repo, upsert_tenant
from leo.db.seed import seed
from leo.fixtures import load_fixture
from leo.match.names import clean_descriptor, descriptor_display_name, match_form, words_agree
from leo.match.transactions import CsvTransactionSource
from leo.match.vendors import DbVendorResolver

SLUG = f"test-{new_uuid7()}"

# Vendor names as the LLM extractor wrote them (data/llm_cache/extract), with the key vendor.
AS_WRITTEN = {
    "CORVANA, INC.": "corvana",
    "Driftwell Analytics": "driftwell",
    "DRIFTWELL ANALYTICS": "driftwell",
    "Ferrolink Networks": "ferrolink",
    "FERROLINK NETWORKS": "ferrolink",
    "Halcyon Payroll Services, Inc.": "halcyon",
    "HALCYON PAYROLL SERVICES, INC.": "halcyon",
    "Harbor Restaurant Supply": "harbor_supply",
    "Kestrel Cloud Hosting, LLC": "kestrel",
    "KESTREL CLOUD HOSTING, LLC": "kestrel",
    "Lumenpost Billing": "lumenpost",
    "Lumenpost": "lumenpost",
    "Quillstack Inc.": "quillstack",
    "QUILLSTACK INC.": "quillstack",
}


@pytest.fixture(scope="module")
def fixture():
    return load_fixture()


# --- Names (no database) ---------------------------------------------------------------------


CLEANED = [
    ("DRIFTWELL ANALYTICS", "DRIFTWELL ANALYTICS"),
    ("CORVANA CRM SUB", "CORVANA CRM SUB"),
    ("HALCYON PAYRL SVCS", "HALCYON PAYRL SVCS"),
    ("QUILLSTACK.IO SUBSCR", "QUILLSTACK.IO SUBSCR"),
    ("KESTREL*CLOUD 844-555-0142", "KESTREL CLOUD"),
    ("FERROLINK NTWKS", "FERROLINK NTWKS"),
    ("PAYWAVE* LUMENPOST", "LUMENPOST"),
    ("SUNRIDGE COFFEE ROASTERS", "SUNRIDGE COFFEE ROASTERS"),
    ("HARBOR REST SUPPLY #12", "HARBOR REST SUPPLY"),
    ("METRO WATER & POWER", "METRO WATER & POWER"),
    ("SQ *BLUE BOTTLE (415) 555-0100", "BLUE BOTTLE"),
]


@pytest.mark.parametrize(("descriptor", "cleaned"), CLEANED)
def test_clean_descriptor(descriptor, cleaned):
    assert clean_descriptor(descriptor) == cleaned


def test_every_csv_descriptor_is_covered(fixture):
    covered = {d for d, _ in CLEANED}
    assert {t.descriptor for t in fixture.transactions} <= covered


def test_match_form_strips_legal_suffixes():
    assert match_form("Halcyon Payroll Services, Inc.") == "halcyon payroll services"
    assert match_form("KESTREL CLOUD HOSTING, LLC") == "kestrel cloud hosting"
    assert match_form("Acme Co. Ltd") == "acme"
    assert match_form("Inc.") == "inc"  # a name that is only a suffix keeps it


SAME = [
    ("halcyon payroll services", "halcyon payrl svcs"),
    ("quillstack", "quillstack io subscr"),
    ("corvana", "corvana crm sub"),
    ("kestrel cloud hosting", "kestrel cloud"),
    ("ferrolink networks", "ferrolink ntwks"),
    ("harbor restaurant supply", "harbor rest supply"),
    ("lumenpost billing", "lumenpost"),
]
DIFFERENT = [
    ("cloud", "kestrel cloud hosting"),
    ("metro cafe", "metro water and power"),
    ("sunridge bakery", "sunridge coffee roasters"),
    ("harbor freight", "harbor restaurant supply"),
    ("lumenpost billing", "kestrel cloud hosting billing"),
]


@pytest.mark.parametrize(("a", "b"), SAME)
def test_words_agree_on_the_same_business(a, b):
    assert words_agree(a, b) and words_agree(b, a)


@pytest.mark.parametrize(("a", "b"), DIFFERENT)
def test_words_disagree_on_a_shared_word_alone(a, b):
    """Each pair scores 0.45 or more on word_similarity, so pg_trgm alone would merge them."""
    assert not words_agree(a, b) and not words_agree(b, a)


def test_descriptor_display_name():
    assert descriptor_display_name("METRO WATER & POWER") == "Metro Water & Power"
    assert descriptor_display_name("SUNRIDGE COFFEE ROASTERS") == "Sunridge Coffee Roasters"


def test_csv_source_reads_the_dataset(fixture):
    source = CsvTransactionSource(fixture.root / "transactions.csv")
    assert source.transactions() == fixture.transactions


# --- Seeded tenant ---------------------------------------------------------------------------


def _key_to_db(session, fixture, tenant: UUID) -> dict[str, UUID]:
    """Key vendor id -> database vendor id, from what the seed resolved: documents' vendors,
    then transactions' (Sunridge and Metro have no documents). Fails if either side maps one
    vendor to two."""
    pairs: set[tuple[str, UUID]] = set()
    docs = dict(
        session.execute(
            select(m.SourceDocument.path, m.SourceDocument.vendor_id).where(
                m.SourceDocument.tenant_id == tenant
            )
        ).all()
    )
    for d in fixture.documents:
        if d.path in docs and d.vendor is not None:
            assert docs[d.path] is not None, d.path
            pairs.add((d.vendor, docs[d.path]))
    txns = dict(
        session.execute(
            select(m.Transaction.external_id, m.Transaction.vendor_id).where(
                m.Transaction.tenant_id == tenant
            )
        ).all()
    )
    for ext, key_vendor in fixture.transaction_vendor_map.items():
        pairs.add((key_vendor, txns[ext]))
    out = dict(pairs)
    assert len(out) == len(pairs), "a key vendor resolved to two vendors"
    assert len(set(out.values())) == len(out), "two key vendors resolved to one"
    return out


@pytest.fixture
def tenant(session):
    return seed(session, slug=SLUG)


def test_every_transaction_resolves_to_its_vendor(session, fixture, tenant):
    ids = _key_to_db(session, fixture, tenant)
    assert len(ids) == len(fixture.vendors) == 10
    vendors = session.scalars(select(m.Vendor.id).where(m.Vendor.tenant_id == tenant)).all()
    assert set(vendors) == set(ids.values())
    names = dict(
        session.execute(
            select(m.Vendor.id, m.Vendor.canonical_name).where(m.Vendor.tenant_id == tenant)
        ).all()
    )
    assert {k: names[v] for k, v in ids.items()} == {
        v.id: v.canonical_name for v in fixture.vendors
    }


def test_plans_match_the_key(session, fixture, tenant):
    rows = session.execute(
        select(m.Transaction.external_id, m.Transaction.plan_label).where(
            m.Transaction.tenant_id == tenant
        )
    ).all()
    assert {ext: label for ext, label in rows if label} == fixture.transaction_plan_map


def test_invoice_facts_match_the_key(session, fixture, tenant):
    ext = dict(
        session.execute(
            select(m.Transaction.id, m.Transaction.external_id).where(
                m.Transaction.tenant_id == tenant
            )
        ).all()
    )
    rows = session.execute(
        select(
            m.InvoiceFact.invoice_number,
            m.InvoiceFact.invoice_date,
            m.InvoiceFact.amount,
            m.InvoiceFact.matches_transaction,
        ).where(m.InvoiceFact.tenant_id == tenant)
    ).all()
    got = {(r.invoice_date, r.amount): ext.get(r.matches_transaction) for r in rows}
    want = {(f.invoice_date, f.amount): f.matches_transaction for f in fixture.invoice_facts}
    assert got == want


def test_zapledger_never_becomes_a_vendor(session, tenant):
    names = session.scalars(
        select(m.Vendor.normalized_name).where(m.Vendor.tenant_id == tenant)
    ).all()
    aliases = session.scalars(
        select(m.VendorAlias.normalized_alias).where(m.VendorAlias.tenant_id == tenant)
    ).all()
    assert not [n for n in [*names, *aliases] if "zapledger" in n]


def test_sunridge_and_metro_come_from_transactions_alone(session, fixture, tenant):
    ids = _key_to_db(session, fixture, tenant)
    for key_vendor in ("sunridge", "metro_utility"):
        vid = ids[key_vendor]
        for model in (m.SourceDocument, m.ContractTerm, m.VendorContact):
            assert not session.scalars(
                select(model.id).where(model.tenant_id == tenant, model.vendor_id == vid)
            ).all(), (key_vendor, model.__tablename__)
        assert session.scalars(select(m.Transaction.id).where(m.Transaction.vendor_id == vid)).all()


# --- Resolver on a fresh tenant --------------------------------------------------------------


def _resolve(session, fixture, names_first: bool) -> dict[str, set[str]]:
    """Key vendor id -> resolved vendor ids, from as-written names and every descriptor."""
    repo = Repo(session, upsert_tenant(session, f"test-{new_uuid7()}", "Resolver test"))
    resolver = DbVendorResolver(repo)
    names = [(n, k, resolver.vendor_for_name) for n, k in AS_WRITTEN.items()]
    descriptors = [
        (t.descriptor, fixture.transaction_vendor_map[t.id], resolver.vendor_for_descriptor)
        for t in fixture.transactions
    ]
    got: dict[str, set[str]] = defaultdict(set)
    for text, key_vendor, resolve in names + descriptors if names_first else descriptors + names:
        got[key_vendor].add(resolve(text))
    for text, key_vendor, _ in names:
        assert resolver.find_vendor(text) in got[key_vendor], text  # now an exact alias hit
    return got


@pytest.mark.parametrize("names_first", [True, False], ids=["names-first", "descriptors-first"])
def test_resolver_partitions_like_the_key(session, fixture, names_first):
    got = _resolve(session, fixture, names_first)
    assert set(got) == {v.id for v in fixture.vendors}
    assert all(len(ids) == 1 for ids in got.values()), got
    assert len({next(iter(ids)) for ids in got.values()}) == 10


def test_find_vendor_never_creates(session):
    repo = Repo(session, upsert_tenant(session, f"test-{new_uuid7()}", "Resolver test"))
    resolver = DbVendorResolver(repo)
    assert resolver.find_vendor("ZapLedger") is None
    assert not session.scalars(
        select(m.Vendor.id).where(m.Vendor.tenant_id == repo.tenant_id)
    ).all()


def test_aliases_grow_and_rerun_is_stable(session):
    repo = Repo(session, upsert_tenant(session, f"test-{new_uuid7()}", "Resolver test"))
    resolver = DbVendorResolver(repo)
    vid = resolver.vendor_for_name("Kestrel Cloud Hosting, LLC")
    assert resolver.vendor_for_descriptor("KESTREL*CLOUD 844-555-0142") == vid
    assert resolver.vendor_for_descriptor("KESTREL*CLOUD 844-555-0142") == vid
    aliases = session.scalars(
        select(m.VendorAlias.alias).where(m.VendorAlias.vendor_id == UUID(vid))
    ).all()
    assert sorted(aliases) == ["KESTREL*CLOUD 844-555-0142", "Kestrel Cloud Hosting, LLC"]


def test_a_shared_word_alone_never_merges(session, fixture):
    """Lookalikes of the dataset's vendors stay separate vendors (review of A5)."""
    repo = Repo(session, upsert_tenant(session, f"test-{new_uuid7()}", "Resolver test"))
    resolver = DbVendorResolver(repo)
    for name in AS_WRITTEN:
        resolver.vendor_for_name(name)
    for t in fixture.transactions:
        resolver.vendor_for_descriptor(t.descriptor)
    for name in ("Cloud", "Metro Cafe", "Sunridge Bakery", "Harbor Freight"):
        assert resolver.find_vendor(name) is None, name
    known = set(session.scalars(select(m.Vendor.id).where(m.Vendor.tenant_id == repo.tenant_id)))
    assert UUID(resolver.vendor_for_descriptor("HARBOR FREIGHT #4")) not in known
