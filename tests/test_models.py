"""A1 schema tests. Need Postgres running and migrated: `make up && make migrate`.

Each test gets the rolled-back `session` fixture from conftest.py, so nothing is left behind.
"""

import datetime as dt

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leo.db.models import (
    Action,
    Base,
    ContractTerm,
    Finding,
    SourceDocument,
    Tenant,
    Vendor,
    new_uuid7,
)


def _tenant(s: Session, name: str = "t") -> Tenant:
    t = Tenant(name=name, slug=f"test-{name}-{new_uuid7()}")
    s.add(t)
    s.flush()
    return t


def _vendor(s: Session, t: Tenant, name: str = "Acme") -> Vendor:
    v = Vendor(tenant_id=t.id, canonical_name=name, normalized_name=name.lower())
    s.add(v)
    s.flush()
    return v


def _doc(s: Session, t: Tenant) -> SourceDocument:
    d = SourceDocument(
        tenant_id=t.id,
        natural_key=f"v1:file:{new_uuid7()}",
        source_kind="file",
        recipient="ap@example.com",
        path="contracts/a.txt",
        storage_path="var/raw/a.txt",
    )
    s.add(d)
    s.flush()
    return d


def _term(s: Session, t: Tenant, v: Vendor, d: SourceDocument, **kw) -> ContractTerm:
    return ContractTerm(
        tenant_id=t.id,
        vendor_id=v.id,
        document_id=d.id,
        natural_key=f"v1:term:{new_uuid7()}",
        type="price",
        value={"amount": "10", "per": "seat_month"},
        **kw,
    )


def _finding(s: Session, t: Tenant, v: Vendor, term: ContractTerm) -> Finding:
    f = Finding(
        tenant_id=t.id,
        vendor_id=v.id,
        natural_key=f"v1:finding:{new_uuid7()}",
        kind="price_increase_notice",
        title="Price went up",
        anchor_term_id=term.id,
    )
    s.add(f)
    s.flush()
    return f


def test_migration_creates_every_table(session):
    tables = set(inspect(session.connection()).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    assert len(tables) == 18


def test_pg_trgm_enabled(session):
    assert session.scalar(text("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'"))


def test_tenant_insert_sets_self_id(session):
    t = _tenant(session)
    assert t.tenant_id == t.id


def test_cross_tenant_reference_rejected(session):
    a, b = _tenant(session, "a"), _tenant(session, "b")
    vendor_b = _vendor(session, b)
    doc_a = _doc(session, a)
    session.add(_term(session, a, vendor_b, doc_a))
    with pytest.raises(IntegrityError, match="fk_contract_term_vendor_id"):
        session.flush()


def test_vendor_upserts_on_normalized_name(session):
    t = _tenant(session)
    stmt = (
        insert(Vendor)
        .values(id=new_uuid7(), tenant_id=t.id, canonical_name="Acme", normalized_name="acme")
        .on_conflict_do_nothing(constraint="uq_vendor_normalized_name")
    )
    session.execute(stmt)
    session.execute(stmt.values(id=new_uuid7()))
    rows = session.scalars(select(Vendor).where(Vendor.tenant_id == t.id)).all()
    assert len(rows) == 1


def test_mutual_supersession_commits(session):
    """v1.superseded_by = v2 and v2.supersedes = v1, inserted v1 first (deferred FKs)."""
    t = _tenant(session)
    v, d = _vendor(session, t), _doc(session, t)
    v1_id, v2_id = new_uuid7(), new_uuid7()
    session.add(_term(session, t, v, d, id=v1_id, status="superseded", superseded_by=v2_id))
    session.flush()
    session.add(_term(session, t, v, d, id=v2_id, supersedes=v1_id))
    session.flush()
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))  # what COMMIT would check


@pytest.mark.parametrize(
    ("state", "approver", "constraint"),
    [
        ("aproved", "lewis", "ck_action_state"),
        ("approved", None, "ck_action_approver"),
        ("sent", None, "ck_action_approver"),
    ],
)
def test_action_state_checks(session, state, approver, constraint):
    t = _tenant(session)
    v, d = _vendor(session, t), _doc(session, t)
    term = _term(session, t, v, d)
    session.add(term)
    session.flush()
    f = _finding(session, t, v, term)
    session.add(
        Action(
            tenant_id=t.id,
            finding_id=f.id,
            natural_key=f"v1:action:{f.natural_key}:negotiation_email",
            action_type="negotiation_email",
            recipient="billing@acme.example",
            subject="Price increase",
            body="Hello",
            state=state,
            approver=approver,
            decided_at=dt.datetime.now(dt.UTC),
        )
    )
    with pytest.raises(IntegrityError, match=constraint):
        session.flush()


def test_approved_action_with_approver_accepted(session):
    t = _tenant(session)
    v, d = _vendor(session, t), _doc(session, t)
    term = _term(session, t, v, d)
    session.add(term)
    session.flush()
    f = _finding(session, t, v, term)
    session.add(
        Action(
            tenant_id=t.id,
            finding_id=f.id,
            natural_key=f"v1:action:{f.natural_key}:negotiation_email",
            action_type="negotiation_email",
            recipient="billing@acme.example",
            subject="Price increase",
            body="Hello",
            state="approved",
            approver="lewis",
        )
    )
    session.flush()  # no IntegrityError
