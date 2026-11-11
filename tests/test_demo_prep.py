"""The demo's loaders: contracts as files into the Gmail tenant, and a first
Gmail import that holds back the email arriving live."""

import importlib.util
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from leo.db.models import SourceDocument
from leo.ingest import folder

ROOT = Path("data/synthetic")
INBOX = ROOT / "inbox"


def _seed_gmail():
    spec = importlib.util.spec_from_file_location("seed_gmail", "scripts/seed_gmail.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_folder_loads_only_the_contracts(session, raw_store):
    tenant_id, new = folder.load(session, ROOT, f"demo-{uuid4().hex[:8]}", raw_store, "contracts")
    paths = session.scalars(
        select(SourceDocument.path).where(SourceDocument.tenant_id == tenant_id)
    ).all()
    expected = sorted(p.name for p in (ROOT / "contracts").iterdir() if not p.name.startswith("."))
    assert new == len(expected) == 6
    assert sorted(paths) == [f"contracts/{n}" for n in expected]


def test_first_import_holds_back_the_live_email():
    sg = _seed_gmail()
    held = [
        "01_lumenpost_price_change_notice.eml",
        "02_fw_lumenpost_price_change_notice.eml",
        "07_kestrel_invoice_2026_10_resynced.eml",
    ]
    names = [p.name for p in sg.select_files(INBOX, skip=held)]
    assert len(names) == 13
    assert not set(held) & set(names)
    assert names == sorted(names)
    live = sg.select_files(INBOX, only=held[0])
    assert [p.name for p in live] == [held[0]]


def test_live_email_gets_a_fresh_message_id_and_keeps_its_date():
    sg = _seed_gmail()
    raw = (INBOX / "01_lumenpost_price_change_notice.eml").read_bytes()
    out = sg.fresh_message_id(raw, "abc123")
    assert b"Message-ID: <live-abc123.20261014091200.price-update.88213@lumenpost.example>" in out
    assert out.count(b"Message-ID:") == 1
    assert out.replace(b"live-abc123.", b"") == raw
    assert sg.fresh_message_id(raw) != sg.fresh_message_id(raw)
