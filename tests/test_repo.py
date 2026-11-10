"""Repository and seed (A2): every write is an upsert, so reruns change nothing.

Needs Postgres running and migrated (`make up && make migrate`). Each test runs in the
rolled-back `session` fixture from conftest.py.
"""

import pytest
from pydantic_core import to_jsonable_python
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from leo.db import keys
from leo.db.models import (
    Action,
    Base,
    Citation,
    ContractTerm,
    Finding,
    Job,
    SourceDocument,
    Vendor,
)
from leo.db.repo import Repo
from leo.db.seed import seed
from leo.fixtures import load_fixture
from leo.types import FindingKind

UNSEEDED = {"extractor_script", "feedback_event", "job"}  # written by A4 and later


def _ids(s: Session) -> dict[str, list]:
    s.flush()
    return {
        name: sorted(s.scalars(select(t.c.id)).all()) for name, t in Base.metadata.tables.items()
    }


def _counts(s: Session) -> dict[str, int]:
    return {name: len(ids) for name, ids in _ids(s).items()}


def _check_deferred(s: Session) -> None:
    """Run the deferred FK checks COMMIT would run."""
    s.flush()
    s.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


@pytest.fixture
def fixture():
    return load_fixture()


def test_seed_twice_gives_same_rows_and_ids(session, fixture):
    first_tenant = seed(session, fixture)
    first = _ids(session)
    assert seed(session, fixture) == first_tenant
    assert _ids(session) == first
    _check_deferred(session)


def test_d5_rerun_adds_no_rows(session, fixture):
    seed(session, fixture)
    first = _counts(session)
    assert all(first[t] > 0 for t in first if t not in UNSEEDED), first
    seed(session, load_fixture())  # a fresh load, like a full rerun
    assert _counts(session) == first
    _check_deferred(session)


def test_d1_resync_is_one_document_row(session, fixture):
    tenant = seed(session, fixture)
    rows = session.scalars(
        select(SourceDocument).where(
            SourceDocument.tenant_id == tenant,
            SourceDocument.filename.in_(
                ["06_kestrel_invoice_2026_10.eml", "07_kestrel_invoice_2026_10_resynced.eml"]
            ),
        )
    ).all()
    assert [r.path for r in rows] == ["inbox/06_kestrel_invoice_2026_10.eml"]
    assert rows[0].notes is None  # 06's notes, not the re-synced copy's
    assert (
        session.scalar(
            select(func.count())
            .select_from(SourceDocument)
            .where(SourceDocument.tenant_id == tenant)
        )
        == len(fixture.documents) - 1
    )


def test_approval_survives_reseed(session, fixture):
    seed(session, fixture)
    action = session.scalars(select(Action).order_by(Action.natural_key)).first()
    body = action.body
    session.execute(
        update(Action).where(Action.id == action.id).values(state="approved", approver="maya")
    )
    for x in fixture.expected_findings:  # every draft changes upstream
        x.expected_action.must_include.append("edited upstream")
    seed(session, fixture)
    session.expire_all()
    again = session.get(Action, action.id)
    assert (again.state, again.approver, again.body) == ("approved", "maya", body)
    _check_deferred(session)


def test_draft_action_takes_new_content(session, fixture):
    seed(session, fixture)
    target = fixture.expected_findings[0]
    target.expected_action.must_include.append("edited upstream")
    seed(session, fixture)
    session.expire_all()
    bodies = session.scalars(select(Action.body)).all()
    assert any(b.endswith("edited upstream") for b in bodies)


def test_shorter_quote_list_leaves_no_stale_citations(session, fixture):
    tenant = seed(session, fixture)
    term = session.scalars(select(ContractTerm).where(ContractTerm.tenant_id == tenant)).first()
    repo = Repo(session, tenant)
    text_ = "first quote and second quote"
    repo.set_citations("term_id", term.id, term.document_id, text_, ["first quote", "second quote"])
    repo.set_citations("term_id", term.id, term.document_id, text_, ["second quote"])
    rows = session.scalars(select(Citation).where(Citation.term_id == term.id)).all()
    assert [(c.ordinal, c.quote, c.start_offset) for c in rows] == [(0, "second quote", 16)]


def test_jsonb_round_trip(session, fixture):
    tenant = seed(session, fixture)
    session.expire_all()
    stored = {
        f.kind: f for f in session.scalars(select(Finding).where(Finding.tenant_id == tenant)).all()
    }
    for f in fixture.findings:
        row = stored[str(f.kind)]
        assert row.impact == to_jsonable_python(f.impact)
        if hasattr(f, "computation"):
            assert row.computation == to_jsonable_python(f.computation)
    escalator = next(f for f in fixture.findings if f.kind == FindingKind.escalator_applied_early)
    assert isinstance(stored[str(escalator.kind)].computation["applied_from"], str)


def test_tenants_stay_apart(session, fixture):
    a = seed(session, fixture)
    b = seed(session, fixture, slug="second-tenant")
    assert a != b
    vendors = {
        t: set(session.scalars(select(Vendor.id).where(Vendor.tenant_id == t)).all())
        for t in (a, b)
    }
    assert vendors[a] and vendors[b] and not vendors[a] & vendors[b]
    _check_deferred(session)


def test_vendor_upsert_without_notes_keeps_them(session, fixture):
    repo = Repo(session, seed(session, fixture))
    vid = repo.upsert_vendor("Acme Widgets", notes="keep")
    assert repo.upsert_vendor("Acme Widgets") == vid
    session.expire_all()
    assert session.get(Vendor, vid).notes == "keep"


def test_job_upsert_bumps_updated_at(session, fixture):
    tenant = seed(session, fixture)
    repo = Repo(session, tenant)
    doc = session.scalars(select(SourceDocument).where(SourceDocument.tenant_id == tenant)).first()
    key = keys.job(doc.natural_key, "extract")
    jid = repo.upsert_job(key, document_id=doc.id, stage="extract")
    session.execute(
        update(Job).where(Job.id == jid).values(updated_at=text("now() - interval '1 hour'"))
    )
    assert repo.upsert_job(key, document_id=doc.id, stage="extract", attempts=1) == jid
    session.expire_all()
    job = session.get(Job, jid)
    assert job.updated_at == session.scalar(select(func.now()))
    assert job.attempts == 1
