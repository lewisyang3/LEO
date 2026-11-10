"""The fixture-backed API (ROADMAP C2). No database needed."""

import pytest
from fastapi.testclient import TestClient

from leo.api.app import app
from leo.api.store import FixtureStore, get_store
from leo.fixtures import load_fixture


@pytest.fixture
def store() -> FixtureStore:
    """A fresh store per test: action state is mutable."""
    return FixtureStore()


@pytest.fixture
def client(store: FixtureStore):
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_findings_sorted_by_days_left_then_at_stake(client):
    rows = client.get("/api/findings").json()
    assert [r["id"] for r in rows] == ["F1", "F3", "F2", "F5", "F4"]
    assert rows[-1]["days_left"] is None
    fixture = load_fixture()
    for r in rows:
        f = next(x for x in fixture.findings if x.id == r["id"])
        assert r["at_stake"] == float(f.impact.headline_amount)


def test_dashboard(client):
    d = client.get("/api/dashboard").json()
    assert d["today"] == "2026-11-02"
    assert d["open_findings"] == 5
    assert d["pending_actions"] == 5


@pytest.mark.parametrize("fid", ["F1", "F2", "F3", "F4", "F5"])
def test_detail_cites_every_quote_and_number(client, fid):
    d = client.get(f"/api/findings/{fid}").json()
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


def test_unknown_finding_404(client):
    assert client.get("/api/findings/F9").status_code == 404


def test_documents_only_from_the_dataset(client):
    path = "contracts/kestrel_master_services_agreement.md"
    d = client.get("/api/documents", params={"path": path}).json()
    assert "Kestrel" in d["text"]
    for bad in ("contracts/nope.md", "../pyproject.toml", "/etc/passwd"):
        assert client.get("/api/documents", params={"path": bad}).status_code == 404


def test_drafts_come_from_the_llm_cache(client, store):
    actions = client.get("/api/actions").json()
    assert len(actions) == 5
    assert all(a["engine"].startswith("llm:") for a in actions)
    assert not any(r["fallback"] for r in store.draft_records.values())


def test_edit_approve_send(client):
    aid = "F1:cancellation_notice"
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


def test_reject_and_restore(client):
    aid = "F4:billing_dispute"
    assert client.post(f"/api/actions/{aid}/restore").status_code == 409  # still a draft
    assert client.post(f"/api/actions/{aid}/reject").json()["state"] == "rejected"
    assert client.post(f"/api/actions/{aid}/approve").status_code == 409
    assert client.post(f"/api/actions/{aid}/restore").json()["state"] == "draft"
    assert client.post(f"/api/actions/{aid}/approve").json()["state"] == "approved"
    client.post(f"/api/actions/{aid}/send")
    assert client.post(f"/api/actions/{aid}/restore").status_code == 409  # sent is final
    assert client.post("/api/actions/nope/approve").status_code == 404


@pytest.mark.parametrize("fid", ["F1", "F2", "F3", "F4", "F5"])
def test_one_headline_equal_to_at_stake(client, fid):
    d = client.get(f"/api/findings/{fid}").json()
    heads = [n for n in d["numbers"] if n["headline"]]
    assert len(heads) == 1
    assert heads[0]["value"] == d["summary"]["at_stake"]


@pytest.mark.parametrize("blank", [{"to": "  "}, {"body": ""}])
def test_edit_rejects_blank_fields(client, blank):
    edit = {"to": "a@b.example", "subject": "S", "body": "B"} | blank
    assert client.patch("/api/actions/F1:cancellation_notice", json=edit).status_code == 422
