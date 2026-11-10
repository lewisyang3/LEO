"""The API on Postgres (ROADMAP C2 routes, C3 store, C4 export, C5 vendors).

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
from leo.db.repo import Repo
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


def test_a_finding_with_no_action_is_still_served(client, session, tenant):
    """The worker writes a finding before its draft (2.1)."""
    fid = _by_kind(client, FindingKind.seats_over_commitment)["id"]
    session.execute(m.Action.__table__.delete().where(m.Action.finding_id == UUID(fid)))
    session.commit()

    row = _by_kind(client, FindingKind.seats_over_commitment)
    assert (row["action_id"], row["action_state"]) == (None, None)
    assert len(client.get("/api/findings").json()) == 5
    detail = client.get(f"/api/findings/{fid}")
    assert detail.status_code == 200 and detail.json()["action"] is None
    assert len(client.get("/api/actions").json()) == 4
    assert client.get("/api/dashboard").json()["open_findings"] == 5
    assert client.get("/api/export/findings.csv").status_code == 200
    vendor = row["vendor"]
    assert client.get(f"/api/vendors/{vendor}").status_code == 200


def test_a_second_action_of_a_finding_can_be_approved(client, session, tenant):
    """Every action of a current finding is served, not only the first (PR #14 review)."""
    fid = _by_kind(client, FindingKind.price_increase_notice)["id"]
    first = session.scalars(select(m.Action).where(m.Action.finding_id == UUID(fid))).one()
    repo = Repo(session, tenant)
    second = repo.upsert_action(
        first.natural_key + ":extra",
        finding_id=first.finding_id,
        action_type="negotiation_email",
        recipient="sales@vendor.example",
        subject="Second",
        body="Another draft.",
    )
    session.commit()

    r = client.post(f"/api/actions/{second}/approve")
    assert r.status_code == 200 and r.json()["state"] == "approved"
    queue = [a["id"] for a in client.get("/api/actions").json()]
    assert queue.index(str(first.id)) + 1 == queue.index(str(second))
    # The finding's row shows its primary (oldest) action.
    assert _by_kind(client, FindingKind.price_increase_notice)["action_id"] == str(first.id)


def test_a_sent_action_stays_in_the_outbox_after_its_finding_is_superseded(client, session):
    """The worker supersedes a finding the reconciler no longer produces; the record that its
    email was sent must stay (2.1 review)."""
    row = _by_kind(client, FindingKind.auto_renewal_deadline)
    aid = row["action_id"]
    client.post(f"/api/actions/{aid}/approve")
    client.post(f"/api/actions/{aid}/send")
    session.execute(
        m.Finding.__table__.update()
        .where(m.Finding.id == UUID(row["id"]))
        .values(status="superseded")
    )
    session.commit()

    outbox = client.get("/api/outbox").json()
    assert [(o["id"], o["state"], o["finding"]) for o in outbox] == [(aid, "sent", row["id"])]
    assert client.post(f"/api/actions/{aid}/send").status_code == 409  # not a 404
    assert aid not in [a["id"] for a in client.get("/api/actions").json()]
    assert len(client.get("/api/findings").json()) == 4


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


# --- Vendors (C5) ---------------------------------------------------------------------------

LUMENPOST_FWD = "inbox/02_fw_lumenpost_price_change_notice.eml"


def _vendor_ids(client) -> dict[str, str]:
    """Fixture vendor slug -> API vendor id, matched by canonical name."""
    by_name = {v["canonical_name"]: v["id"] for v in client.get("/api/vendors").json()}
    return {v.id: by_name[v.canonical_name] for v in load_fixture().vendors}


def _vendor(client, slug: str) -> dict:
    r = client.get(f"/api/vendors/{_vendor_ids(client)[slug]}")
    assert r.status_code == 200
    return r.json()


def _history(detail: dict) -> dict[str | None, list[tuple[str, float, bool, float | None]]]:
    """plan label -> [(effective date, amount, in effect, change)]"""
    return {
        s["plan_label"]: [
            (
                p["term"]["term"]["effective_date"],
                p["term"]["term"]["value"]["amount"],
                p["in_effect"],
                p["change"],
            )
            for p in s["points"]
        ]
        for s in detail["price_history"]
    }


def test_vendor_list_counts_match_the_key(client):
    fixture = load_fixture()
    rows = {v["id"]: v for v in client.get("/api/vendors").json()}
    ids = _vendor_ids(client)
    assert len(rows) == len(fixture.vendors) == 10
    for v in fixture.vendors:
        row = rows[ids[v.id]]
        assert row["terms"] == sum(t.vendor == v.id for t in fixture.terms), v.id
        txns = sum(vid == v.id for vid in fixture.transaction_vendor_map.values())
        assert row["transactions"] == txns, v.id
        assert row["open_findings"] == sum(f.vendor == v.id for f in fixture.findings), v.id
    dash = client.get("/api/dashboard").json()
    assert sum(r["open_findings"] for r in rows.values()) == dash["open_findings"]
    assert sum(r["at_stake"] for r in rows.values()) == pytest.approx(dash["at_stake"])


def test_lumenpost_price_history_is_d4(client):
    """D4: two plans, a change on one only, histories kept apart. Today is 2026-11-02, so the
    Dec 1 prices are known but not in effect."""
    d = _vendor(client, "lumenpost")
    assert _history(d) == {
        "Growth": [("2025-03-03", 299.0, True, None), ("2026-12-01", 339.0, False, 40.0)],
        "Transactional Email add-on": [
            ("2025-03-03", 49.0, True, None),
            ("2026-12-01", 49.0, False, 0.0),
        ],
    }
    assert {s["agreement_ref"] for s in d["price_history"]} == {"LP-204417"}
    assert d["linked_duplicates"] == [LUMENPOST_FWD]
    assert [f["kind"] for f in d["findings"]] == ["price_increase_notice"]


def test_price_history_leaves_out_linked_duplicates(client, session, tenant):
    """D2: a term extracted from the forward is counted once, under the original."""
    fwd = session.scalar(
        select(m.SourceDocument).where(
            m.SourceDocument.tenant_id == tenant, m.SourceDocument.path == LUMENPOST_FWD
        )
    )
    quote = "the price of the Growth plan will change from $299/month to $339/month"
    repo = Repo(session, tenant)
    tid = repo.upsert_term(
        "test:c5:fwd-growth-price",
        vendor_id=fwd.vendor_id,
        document_id=fwd.id,
        type="price",
        value={"amount": 339.0, "currency": "USD", "per": "month"},
        plan_label="Growth",
        agreement_ref="LP-204417",
        effective_date=datetime.date(2026, 12, 1),
    )
    repo.set_citations("term_id", tid, fwd.id, quote, [quote])
    session.flush()

    d = _vendor(client, "lumenpost")
    assert [p[1] for p in _history(d)["Growth"]] == [299.0, 339.0]
    assert str(tid) not in {t["term"]["id"] for t in d["terms"]}
    assert len(d["terms"]) == sum(t.vendor == "lumenpost" for t in load_fixture().terms)
    row = next(v for v in client.get("/api/vendors").json() if v["id"] == str(fwd.vendor_id))
    assert row["terms"] == len(d["terms"])


def test_unvalidated_items_stay_out_of_history_and_matches(client, session, tenant):
    """`needs_review` items failed validation. The reconciler ignores them, so the price history
    and the invoice matches do too. The terms list keeps every status."""

    def doc(path: str) -> m.SourceDocument:
        return session.scalar(
            select(m.SourceDocument).where(
                m.SourceDocument.tenant_id == tenant, m.SourceDocument.path == path
            )
        )

    repo = Repo(session, tenant)
    order = doc("inbox/03_lumenpost_order_confirmation.eml")
    tid = repo.upsert_term(
        "test:c5:unvalidated-growth-price",
        vendor_id=order.vendor_id,
        document_id=order.id,
        type="price",
        value={"amount": 999.0, "currency": "USD", "per": "month"},
        plan_label="Growth",
        agreement_ref="LP-204417",
        effective_date=datetime.date(2026, 6, 1),
        status="needs_review",
    )
    repo.set_citations("term_id", tid, order.id, "Growth: $999/month", ["Growth: $999/month"])

    invoice = doc("inbox/05_kestrel_invoice_2026_09.eml")
    june = session.scalar(
        select(m.Transaction.id).where(
            m.Transaction.tenant_id == tenant,
            m.Transaction.vendor_id == invoice.vendor_id,
            m.Transaction.date == datetime.date(2026, 6, 1),
        )
    )
    fid = repo.upsert_invoice_fact(
        "test:c5:unvalidated-invoice",
        vendor_id=invoice.vendor_id,
        document_id=invoice.id,
        billing_period="2026-06",
        invoice_date=datetime.date(2026, 6, 1),
        amount=Decimal("1200"),
        invoice_number="INV-BAD-0601",
        matches_transaction=june,
        status="needs_review",
    )
    repo.set_citations("invoice_fact_id", fid, invoice.id, "INV-BAD-0601", ["INV-BAD-0601"])
    session.flush()

    lp = _vendor(client, "lumenpost")
    growth = _history(lp)["Growth"]
    assert [(p[1], p[3]) for p in growth] == [(299.0, None), (339.0, 40.0)]
    term = next(t["term"] for t in lp["terms"] if t["term"]["id"] == str(tid))
    assert term["status"] == "needs_review"

    kc = _vendor(client, "kestrel")
    charge = next(x for x in kc["transactions"] if x["transaction"]["id"] == str(june))
    assert charge["invoice_fact"] is None


def test_kestrel_transactions_matched_to_invoices(client):
    fixture = load_fixture()
    d = _vendor(client, "kestrel")
    txns = d["transactions"]
    key = sorted(
        (t for t in fixture.transactions if fixture.transaction_vendor_map[t.id] == "kestrel"),
        key=lambda t: t.date,
    )
    assert [(x["transaction"]["date"], x["transaction"]["amount"]) for x in txns] == [
        (t.date.isoformat(), float(t.amount)) for t in key
    ]
    # API ids are UUIDs: match the key's invoice -> transaction pairs by date and amount.
    key_txn = {t.id: (t.date.isoformat(), float(t.amount)) for t in key}
    expected = {
        key_txn[f.matches_transaction]: f.invoice_number
        for f in fixture.invoice_facts
        if f.vendor == "kestrel"
    }
    got = {
        (x["transaction"]["date"], x["transaction"]["amount"]): x["invoice_fact"]["invoice_number"]
        for x in txns
        if x["invoice_fact"]
    }
    assert (
        got
        == expected
        == {
            ("2026-09-01", 1308.0): "INV-KCH-260901",
            ("2026-10-01", 1308.0): "INV-KCH-261001",
        }
    )
    # Every finding a charge cites is one of this vendor's findings.
    finding_ids = {f["id"] for f in d["findings"]}
    cited = {fid for x in txns for fid in x["findings"]}
    assert cited and cited <= finding_ids


def test_vendor_terms_every_key_term_once(client):
    fixture = load_fixture()
    for slug in _vendor_ids(client):
        d = _vendor(client, slug)
        got = sorted((t["term"]["type"], t["citations"][0]["quote"]) for t in d["terms"])
        key = sorted((str(t.type), t.quotes[0]) for t in fixture.terms if t.vendor == slug)
        assert got == key, slug


@pytest.mark.parametrize("bad", ["nope", "00000000-0000-0000-0000-000000000000"])
def test_unknown_vendor_404(client, bad):
    assert client.get(f"/api/vendors/{bad}").status_code == 404


def test_vendor_from_another_tenant_404(client, session):
    other = seed(session, slug=f"test-{new_uuid7()}")
    vid = session.scalar(select(m.Vendor.id).where(m.Vendor.tenant_id == other).limit(1))
    assert client.get(f"/api/vendors/{vid}").status_code == 404


# --- B6 analytics ------------------------------------------------------------------------


def test_analytics_summary_and_job_report_spend(client, session, tenant):
    from leo.worker import queue

    queue.request_reconcile(session, tenant)  # a tenant job: document_id is NULL
    job = session.scalar(select(m.Job).where(m.Job.tenant_id == tenant))
    execution = m.StageExecution(
        tenant_id=tenant, job_id=job.id, stage=job.stage, status="completed"
    )
    session.add(execution)
    session.flush()
    session.add(
        m.ModelUsage(
            tenant_id=tenant,
            stage_execution_id=execution.id,
            provider="claude",
            model="claude-haiku-5-5",
            attempt_number=1,
            input_tokens=450,
            output_tokens=900,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=5125,
            cost_usd=Decimal("0.00051125"),
            status="success",
        )
    )
    session.commit()

    summary = client.get("/api/analytics/summary").json()
    assert summary["reached_by_stage"] == {"reconcile": 1}
    assert summary["providers"] == [
        {
            "provider": "claude",
            "api_attempts": 1,
            "input_tokens": 450,
            "output_tokens": 900,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 5125,
            "known_cost_usd": "0.00051125",
            "unpriced_attempts": 0,
        }
    ]

    report = client.get(f"/api/analytics/jobs/{job.id}").json()
    assert report["document_id"] is None
    [call] = report["executions"][0]["api_calls"]
    assert (call["cache_read_input_tokens"], call["cost_usd"]) == (5125, "0.00051125")
