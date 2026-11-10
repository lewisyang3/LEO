"""Duplicate linking (A6): D2 linked, D3 kept apart, D4 histories kept apart, reruns stable.

The database tests need Postgres running and migrated (`make up && make migrate`), and run in
the rolled-back `session` fixture from conftest.py.
"""

import datetime as dt
from decimal import Decimal
from email.message import EmailMessage
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update

from leo.db import keys
from leo.db import models as m
from leo.db.models import new_uuid7
from leo.db.read import load_findings, load_ledger, load_price_history
from leo.db.repo import Repo, upsert_tenant
from leo.db.seed import seed
from leo.fixtures import DATA_DIR, load_fixture
from leo.ingest.ingest import ingest
from leo.match.duplicates import (
    PROBABLE_DUPLICATE,
    context_fingerprint,
    is_forward,
    link_duplicates,
)
from leo.reconcile.reconciler import RuleReconciler
from leo.types import FindingKind

ORIGINAL = "inbox/01_lumenpost_price_change_notice.eml"
FORWARD = "inbox/02_fw_lumenpost_price_change_notice.eml"
SEPT = "inbox/05_kestrel_invoice_2026_09.eml"
OCT = "inbox/06_kestrel_invoice_2026_10.eml"


def _read(path: str) -> str:
    return (DATA_DIR / path).read_text()


# --- Pure functions --------------------------------------------------------------------------


def test_forward_check():
    assert not is_forward("Important update to your Lumenpost plan pricing", _read(ORIGINAL))
    assert is_forward("Fwd: Important update", "")
    assert is_forward("FW: invoice", "")
    assert is_forward("Re: x", "hi\n\n---------- Forwarded message ---------\nFrom: a\n")
    assert is_forward(None, "-----Original Message-----\n")
    assert not is_forward("Forwarding address update", "nothing forwarded here")


def test_fingerprint_is_order_free_and_tells_invoices_apart():
    price = {"amount": "339", "currency": "USD", "per": "month"}
    a = ("v", "price", price, "Growth", None, dt.date(2026, 12, 1))
    b = ("v", "price", {"amount": "49", "currency": "USD", "per": "month"}, None, None, None)
    assert context_fingerprint([a, b], []) == context_fingerprint([b, a], [])
    assert context_fingerprint([], []) is None
    sept = ("v", "INV-1", "2026-09", dt.date(2026, 9, 1), "100")
    octo = ("v", "INV-2", "2026-10", dt.date(2026, 10, 1), "100")
    assert context_fingerprint([], [sept]) != context_fingerprint([], [octo])


# --- On the seeded answer key ----------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture():
    return load_fixture()


@pytest.fixture
def seeded(session, fixture):
    tenant = seed(session, fixture, slug=f"test-{new_uuid7()}")
    session.flush()
    return tenant


def _docs(session, tenant: UUID) -> dict[str, m.SourceDocument]:
    q = select(m.SourceDocument).where(m.SourceDocument.tenant_id == tenant)
    return {d.path: d for d in session.scalars(q)}


def _dup_links(session, tenant: UUID) -> list[m.DocumentLink]:
    q = select(m.DocumentLink).where(
        m.DocumentLink.tenant_id == tenant, m.DocumentLink.link_type == PROBABLE_DUPLICATE
    )
    return list(session.scalars(q))


def test_d2_forward_is_linked_and_counted_once(session, seeded, fixture):
    docs = _docs(session, seeded)
    fwd, orig = docs[FORWARD], docs[ORIGINAL]
    [link] = _dup_links(session, seeded)
    assert (link.document_id, link.linked_document_id) == (fwd.id, orig.id)
    assert fwd.context_fingerprint == orig.context_fingerprint == link.context_fingerprint
    # The forward has its own stored items, cited in its own text ...
    stored = select(m.ContractTerm).where(m.ContractTerm.document_id == fwd.id)
    assert len(list(session.scalars(stored))) == len(fixture.terms_for(ORIGINAL)) > 0
    # ... which the ledger leaves out.
    ledger = load_ledger(session, seeded)
    assert not [t for t in ledger.terms if t.document == FORWARD]
    assert not [f for f in ledger.invoice_facts if f.document == FORWARD]
    rec = RuleReconciler().reconcile(ledger, fixture.demo_today)
    [f2] = [f for f in rec.findings if f.kind == FindingKind.price_increase_notice]
    assert f2.linked_duplicates == [FORWARD]


def test_d3_monthly_invoices_stay_separate(session, seeded):
    docs = _docs(session, seeded)
    sept, octo = docs[SEPT], docs[OCT]
    assert sept.context_fingerprint and octo.context_fingerprint
    assert sept.context_fingerprint != octo.context_fingerprint
    linked = {x.document_id for x in _dup_links(session, seeded)}
    assert not linked & {sept.id, octo.id}


def test_no_items_no_fingerprint(session, seeded):
    assert _docs(session, seeded)["inbox/04_lumenpost_newsletter.eml"].context_fingerprint is None


def test_d4_two_plans_keep_separate_histories(session, seeded):
    docs = _docs(session, seeded)
    vendor = docs[ORIGINAL].vendor_id
    terms = {t.id: t for t in load_ledger(session, seeded).terms}
    history = {
        s.plan_label: [(terms[t].value.amount, terms[t].effective_date) for t in s.terms]
        for s in load_price_history(session, seeded, vendor)
    }
    assert history["Growth"] == [
        (Decimal("299"), dt.date(2025, 3, 3)),
        (Decimal("339"), dt.date(2026, 12, 1)),
    ]
    addon = [k for k in history if k != "Growth"]
    assert len(addon) == 1
    assert {amount for amount, _ in history[addon[0]]} == {Decimal("49")}


def test_rerun_keeps_link_and_stale_link_is_removed(session, seeded, fixture, raw_store):
    repo = Repo(session, seeded)
    [before] = _dup_links(session, seeded)
    assert link_duplicates(repo, raw_store) == {before.document_id: before.linked_document_id}
    [after] = _dup_links(session, seeded)
    assert after.id == before.id
    # The forward's items change: no longer the same fingerprint, so no longer linked.
    fwd = _docs(session, seeded)[FORWARD].id
    one = session.scalars(
        select(m.ContractTerm.id).where(m.ContractTerm.document_id == fwd)
    ).first()
    session.execute(delete(m.Citation).where(m.Citation.term_id == one))
    session.execute(delete(m.ContractTerm).where(m.ContractTerm.id == one))
    assert link_duplicates(repo, raw_store) == {}
    assert _dup_links(session, seeded) == []


# --- Attachment signal (the dataset has none) ------------------------------------------------


def _email(subject: str, date: str, attachment: bytes | None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "billing@acme.example"
    msg["To"] = "maya@brightwater.example"
    msg["Subject"] = subject
    msg["Date"] = date
    msg.set_content("Your order is attached.\n")
    if attachment is not None:
        msg.add_attachment(attachment, maintype="text", subtype="plain", filename="order.txt")
    return msg.as_bytes()


def test_same_attachment_links_a_resend_and_nothing_else_does(session, raw_store):
    repo = Repo(session, upsert_tenant(session, f"test-{new_uuid7()}", "Dup test"))
    vendor = repo.upsert_vendor("Acme")
    first = ingest(
        repo, raw_store, _email("Order", "Mon, 5 Oct 2026 09:00:00 +0000", b"PO 1"), "a.eml"
    )
    resend = ingest(
        repo, raw_store, _email("Order", "Tue, 6 Oct 2026 09:00:00 +0000", b"PO 1"), "b.eml"
    )
    other = ingest(
        repo, raw_store, _email("Order", "Wed, 7 Oct 2026 09:00:00 +0000", None), "c.eml"
    )
    for r in (first, resend, other):
        repo.upsert_term(
            keys.term(r.natural_key, "price", "0:1"),
            vendor_id=vendor,
            document_id=r.document_id,
            type="price",
            value={"amount": "10", "currency": "USD", "per": "month"},
            status="current",
        )
    session.flush()
    assert link_duplicates(repo, raw_store) == {resend.document_id: first.document_id}


def test_forwards_with_no_original_link_to_the_earliest(session, raw_store):
    repo = Repo(session, upsert_tenant(session, f"test-{new_uuid7()}", "Dup test"))
    vendor = repo.upsert_vendor("Acme")
    a = ingest(
        repo, raw_store, _email("Fwd: Price", "Mon, 5 Oct 2026 09:00:00 +0000", None), "a.eml"
    )
    b = ingest(
        repo, raw_store, _email("FW: Price", "Tue, 6 Oct 2026 09:00:00 +0000", None), "b.eml"
    )
    for r in (a, b):
        repo.upsert_term(
            keys.term(r.natural_key, "price", "0:1"),
            vendor_id=vendor,
            document_id=r.document_id,
            type="price",
            value={"amount": "10", "currency": "USD", "per": "month"},
            status="current",
        )
    session.flush()
    assert link_duplicates(repo, raw_store) == {b.document_id: a.document_id}


def test_findings_still_load_when_they_cite_a_linked_duplicate(session, seeded):
    """A finding written before its document was linked (2.1's worker order) still reads."""
    fwd = _docs(session, seeded)[FORWARD].id
    q = select(m.ContractTerm.id).where(
        m.ContractTerm.document_id == fwd, m.ContractTerm.plan_label == "Growth"
    )
    fwd_term = session.scalars(q).one()
    session.execute(
        update(m.Finding)
        .where(m.Finding.tenant_id == seeded, m.Finding.kind == FindingKind.price_increase_notice)
        .values(anchor_term_id=fwd_term)
    )
    [f2] = [
        f for f in load_findings(session, seeded) if f.kind == FindingKind.price_increase_notice
    ]
    assert f2.anchor.term == str(fwd_term)
    assert FORWARD in f2.documents
