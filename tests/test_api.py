"""The API on Postgres (ROADMAP C2 routes, C3 store).

Needs `make up && make migrate`. Each test seeds the answer key under its own random tenant
slug, inside the rolled-back `session` from conftest.py. The dev database that `make seed`
and `make api` commit to is never touched, so an approval made in the UI can't break a test.
"""

import csv
import datetime
import io
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from leo.api.app import app
from leo.api.export import BOM, FINDING_COLUMNS, TERM_COLUMNS, _cell
from leo.api.routes import get_session
from leo.config import get_settings
from leo.db import models as m
from leo.db.models import new_uuid7
from leo.db.read import load_findings, load_ledger
from leo.db.seed import seed
from leo.fixtures import load_fixture
from leo.types import FindingKind

KIND_ORDER = [
    FindingKind.auto_renewal_deadline,  # F1
    FindingKind.price_cap_exceeded,  # F3
    FindingKind.price_increase_notice,  # F2
    FindingKind.escalator_applied_early,  # F5
    FindingKind.seats_over_commitment,  # F4
]
KESTREL_DOC = "contracts/kestrel_master_services_agreement.md"


@pytest.fixture
def tenant(session, raw_store, monkeypatch):
    """Seed under a random slug, commit (releases the savepoint; the outer transaction still
    rolls back), and point the API at it."""
    slug = f"test-{new_uuid7()}"
    tenant_id = seed(session, slug=slug)
    session.commit()
    monkeypatch.setenv("LEO_TENANT", slug)
    monkeypatch.setenv("LEO_TODAY", "2026-11-02")
    get_settings.cache_clear()
    return tenant_id


@pytest.fixture
def client(session, tenant):
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _by_kind(client, kind: FindingKind) -> dict:
    return next(r for r in client.get("/api/findings").json() if r["kind"] == kind)


def _action(client, kind: FindingKind) -> str:
    return _by_kind(client, kind)["action_id"]


# --- Reads ----------------------------------------------------------------------------------


def test_findings_sorted_by_days_left_then_at_stake(client):
    rows = client.get("/api/findings").json()
    assert [r["kind"] for r in rows] == KIND_ORDER
    assert rows[-1]["days_left"] is None
    fixture = load_fixture()
    for r in rows:
        f = next(x for x in fixture.findings if x.kind == r["kind"])
        assert r["at_stake"] == float(f.impact.headline_amount)


def test_dashboard(client):
    d = client.get("/api/dashboard").json()
    assert d["today"] == "2026-11-02"
    assert d["open_findings"] == 5
    assert d["pending_actions"] == 5


@pytest.mark.parametrize("kind", KIND_ORDER)
def test_detail_cites_every_quote_and_number(client, kind):
    d = client.get(f"/api/findings/{_by_kind(client, kind)['id']}").json()
    finding = d["finding"]
    for item in [*d["terms"], *d["invoice_facts"]]:
        assert item["citations"]
        for c in item["citations"]:
            assert c["start"] is not None, c
    linked = {*finding["terms"], *finding["invoice_facts"]}
    keys = {n["key"] for n in d["numbers"]}
    for n in d["numbers"]:
        assert n["sources"] and set(n["sources"]) <= linked, n
    for group in ("impact", "computation"):
        for field in finding.get(group, {}):
            if field != "basis":  # F1's impact.basis is the only non-number field
                assert f"{group}.{field}" in keys
    if finding["deadline"]:
        assert "deadline" in keys


@pytest.mark.parametrize("kind", KIND_ORDER)
def test_one_headline_equal_to_at_stake(client, kind):
    d = client.get(f"/api/findings/{_by_kind(client, kind)['id']}").json()
    heads = [n for n in d["numbers"] if n["headline"]]
    assert len(heads) == 1
    assert heads[0]["value"] == d["summary"]["at_stake"]


def test_citation_offsets_index_the_document_text(client):
    d = client.get(f"/api/findings/{_by_kind(client, FindingKind.price_cap_exceeded)['id']}")
    for item in d.json()["terms"]:
        for c in item["citations"]:
            text = client.get("/api/documents", params={"path": c["document"]}).json()["text"]
            assert text[c["start"] : c["end"]] == c["quote"]


@pytest.mark.parametrize("bad", ["F9", "nope", str(new_uuid7())])
def test_unknown_finding_404(client, bad):
    assert client.get(f"/api/findings/{bad}").status_code == 404


def test_documents_only_from_the_tenant(client):
    d = client.get("/api/documents", params={"path": KESTREL_DOC}).json()
    assert "Kestrel" in d["text"]
    for bad in ("contracts/nope.md", "../pyproject.toml", "/etc/passwd"):
        assert client.get("/api/documents", params={"path": bad}).status_code == 404
    # D1's re-synced copy is the original's row, which keeps the original's path.
    resynced = "inbox/07_kestrel_invoice_2026_10_resynced.eml"
    assert client.get("/api/documents", params={"path": resynced}).status_code == 404


def test_seed_drafts_show_the_fixture_engine(client):
    actions = client.get("/api/actions").json()
    assert len(actions) == 5
    assert {a["engine"] for a in actions} == {"fixture"}


def test_missing_tenant_is_503(client, monkeypatch):
    monkeypatch.setenv("LEO_TENANT", f"test-{new_uuid7()}")
    get_settings.cache_clear()
    r = client.get("/api/findings")
    assert r.status_code == 503 and "make seed" in r.json()["detail"]


# --- The DB round trip ----------------------------------------------------------------------


def test_load_findings_matches_the_key(session, tenant):
    """Every field but the ids: ids are compared by what they name. Link order isn't stored,
    so lists are compared as sets."""
    fixture = load_fixture()
    ledger = load_ledger(session, tenant)
    db_terms = {t.id: (t.document, str(t.type), t.quotes[0]) for t in ledger.terms}
    db_facts = {f.id: (f.document, f.quotes[0]) for f in ledger.invoice_facts}
    db_vendors = {v.id: v.canonical_name for v in ledger.vendors}
    ext = dict(session.execute(select(m.Transaction.id, m.Transaction.external_id)).all())
    db_txns = {str(k): v for k, v in ext.items()}
    key_terms = {t.id: (t.document, str(t.type), t.quotes[0]) for t in fixture.terms}
    key_facts = {f.id: (f.document, f.quotes[0]) for f in fixture.invoice_facts}
    key_vendors = {v.id: v.canonical_name for v in fixture.vendors}

    def norm(f, terms, facts, txns, vendors) -> dict:
        d = f.model_dump(mode="json")
        basis = d["deadline_basis"]
        return {
            **{k: v for k, v in d.items() if k not in ("id", "title", "deadline_notes")},
            "vendor": vendors[d["vendor"]],
            "terms": {terms[t] for t in d["terms"]},
            "transactions": {txns(t) for t in d["transactions"]},
            "invoice_facts": {facts[x] for x in d["invoice_facts"]},
            "documents": set(d["documents"]),
            "linked_duplicates": set(d["linked_duplicates"]),
            "anchor": (
                terms[d["anchor"]["term"]],
                txns(d["anchor"]["transaction"]) if d["anchor"]["transaction"] else None,
            ),
            "deadline_basis": basis
            and (
                {terms[t] for t in basis["terms"]},
                facts[basis["invoice_fact"]] if basis["invoice_fact"] else None,
            ),
        }

    got = {
        f.kind: norm(f, db_terms, db_facts, db_txns.__getitem__, db_vendors)
        for f in load_findings(session, tenant, ledger)
    }
    want = {
        f.kind: norm(f, key_terms, key_facts, lambda t: t, key_vendors) for f in fixture.findings
    }
    # The key gives F4 an empty basis; the DB stores only flagged links, so it reads back None.
    for d in (got, want):
        b = d[FindingKind.seats_over_commitment]["deadline_basis"]
        d[FindingKind.seats_over_commitment]["deadline_basis"] = b if b and any(b) else None
    assert got == want


# --- Actions --------------------------------------------------------------------------------


def test_edit_approve_send(client, session):
    aid = _action(client, FindingKind.auto_renewal_deadline)
    r = client.patch(f"/api/actions/{aid}", json={"to": "x@y.example", "subject": "S", "body": "B"})
    assert r.status_code == 200 and r.json()["edited"] is True
    assert client.post(f"/api/actions/{aid}/send").status_code == 409  # not approved yet
    assert client.post(f"/api/actions/{aid}/approve").json()["state"] == "approved"
    assert (
        client.patch(
            f"/api/actions/{aid}", json={"to": "a", "subject": "b", "body": "c"}
        ).status_code
        == 409
    )
    sent = client.post(f"/api/actions/{aid}/send").json()
    assert sent["state"] == "sent" and sent["sent_at"]
    outbox = client.get("/api/outbox").json()
    assert [(o["id"], o["body"]) for o in outbox] == [(aid, "B")]
    assert client.get("/api/findings").json()[0]["action_state"] == "sent"

    # The decision is in the database: a new session sees it.
    with Session(bind=session.connection()) as fresh:
        row = fresh.get(m.Action, UUID(aid))
        assert (row.state, row.approver, row.recipient) == ("sent", "reviewer", "x@y.example")
        assert row.decided_at and row.sent_at and row.edited_at


def test_reject_and_restore(client):
    aid = _action(client, FindingKind.seats_over_commitment)
    assert client.post(f"/api/actions/{aid}/restore").status_code == 409  # still a draft
    assert client.post(f"/api/actions/{aid}/reject").json()["state"] == "rejected"
    assert client.post(f"/api/actions/{aid}/approve").status_code == 409
    assert client.post(f"/api/actions/{aid}/restore").json()["state"] == "draft"
    assert client.post(f"/api/actions/{aid}/approve").json()["state"] == "approved"
    client.post(f"/api/actions/{aid}/send")
    assert client.post(f"/api/actions/{aid}/restore").status_code == 409  # sent is final
    assert client.post("/api/actions/nope/approve").status_code == 404
    assert client.post(f"/api/actions/{new_uuid7()}/approve").status_code == 404


def test_decisions_and_edits_survive_a_reseed(client, session):
    edited = _action(client, FindingKind.price_increase_notice)
    approved = _action(client, FindingKind.price_cap_exceeded)
    client.patch(f"/api/actions/{edited}", json={"to": "a@b.example", "subject": "S", "body": "B"})
    client.post(f"/api/actions/{approved}/approve")
    seed(session, slug=get_settings().leo_tenant)
    session.commit()
    actions = {a["id"]: a for a in client.get("/api/actions").json()}
    assert (actions[edited]["body"], actions[edited]["edited"]) == ("B", True)
    assert actions[approved]["state"] == "approved"


@pytest.mark.parametrize("blank", [{"to": "  "}, {"body": ""}])
def test_edit_rejects_blank_fields(client, blank):
    aid = _action(client, FindingKind.auto_renewal_deadline)
    edit = {"to": "a@b.example", "subject": "S", "body": "B"} | blank
    assert client.patch(f"/api/actions/{aid}", json=edit).status_code == 422


# --- Upload and stats -----------------------------------------------------------------------

EML = (
    b"From: Billing <billing@newvendor.example>\r\n"
    b"To: ap@brightwater.example\r\n"
    b"Subject: Your invoice\r\n"
    b"Date: Mon, 02 Nov 2026 09:00:00 +0000\r\n"
    b"\r\n"
    b"Invoice total: $120.00\r\n"
)


def test_upload_stores_once(client):
    files = {"file": ("new_invoice.eml", EML, "message/rfc822")}
    first = client.post("/api/documents", files=files).json()
    assert first["created"] is True
    assert first["path"].startswith("uploads/") and first["path"].endswith("-new_invoice.eml")
    again = client.post("/api/documents", files=files).json()
    assert (again["created"], again["document_id"]) == (False, first["document_id"])
    text = client.get("/api/documents", params={"path": first["path"]}).json()["text"]
    assert "Invoice total" in text
    assert client.get("/api/stats").json()["documents"] == 22


def test_upload_of_seeded_bytes_keeps_its_path(client):
    raw = (load_fixture().root / KESTREL_DOC).read_bytes()
    r = client.post("/api/documents", files={"file": ("copy.md", raw, "text/markdown")}).json()
    assert (r["created"], r["path"]) == (False, KESTREL_DOC)


def test_upload_rejects_an_empty_file(client):
    r = client.post("/api/documents", files={"file": ("empty.eml", b"", "message/rfc822")})
    assert r.status_code == 422


def test_stats_count_the_seed(client):
    s = client.get("/api/stats").json()
    assert s["documents"] == 21  # D1's re-synced copy is the original's row
    assert sum(s["documents_by_class"].values()) == 21
    assert s["findings"] == 5
    assert s["actions_by_state"] == {"draft": 5}
    assert s["jobs"] == []  # the worker (A4) writes jobs


# --- Export (C4) ----------------------------------------------------------------------------


def _csv(client, name: str) -> list[dict[str, str]]:
    r = client.get(f"/api/export/{name}.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == f'attachment; filename="leo-{name}-2026-11-02.csv"'
    assert r.text.startswith(BOM)
    reader = csv.DictReader(io.StringIO(r.text.removeprefix(BOM)))
    rows = list(reader)
    assert reader.fieldnames == (TERM_COLUMNS if name == "terms" else FINDING_COLUMNS)
    return rows


def test_export_terms_has_every_term(client):
    rows = _csv(client, "terms")
    fixture = load_fixture()
    assert len(rows) == len(fixture.terms)

    key_cap = next(t for t in fixture.terms if t.type == "price_cap" and t.document == KESTREL_DOC)
    cap = next(r for r in rows if r["type"] == "price_cap" and r["document"] == KESTREL_DOC)
    assert cap["max_increase_pct"] == str(key_cap.value.max_increase_pct)
    assert cap["amount"] == ""  # not a price_cap key

    superseded = [r for r in rows if r["status"] == "superseded"]
    assert superseded and all(r["superseded_by"] for r in superseded)
    ids = {r["id"] for r in rows}
    assert {r["superseded_by"] for r in superseded} <= ids


def test_export_findings_in_dashboard_order(client):
    rows = _csv(client, "findings")
    assert [r["kind"] for r in rows] == KIND_ORDER
    assert [r["id"] for r in rows] == [s["id"] for s in client.get("/api/findings").json()]
    fixture = load_fixture()
    for r in rows:
        f = next(x for x in fixture.findings if x.kind == r["kind"])
        assert r["at_stake"] == str(f.impact.headline_amount)
        assert r["action_state"] == "draft"
        assert r["impact"]


def test_export_cell_formatting():
    assert _cell("=SUM(A1)") == "'=SUM(A1)"
    assert _cell("-1 for you") == "'-1 for you"
    assert _cell("@here") == "'@here"
    assert _cell(Decimal("-12.50")) == "-12.50"
    assert _cell(None) == ""
    assert _cell(True) == "true"
    assert _cell(datetime.date(2026, 11, 2)) == "2026-11-02"
    assert _cell(FindingKind.price_cap_exceeded) == "price_cap_exceeded"
