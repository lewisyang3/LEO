"""`ingest()` (A3.3) and the folder loaders (A3.4).

Needs Postgres running and migrated (`make up && make migrate`).
"""

import hashlib
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from leo.db import keys
from leo.db.models import DocumentLink, SourceDocument
from leo.db.repo import Repo, upsert_tenant
from leo.eval.holdout import HOLDOUT_DIR
from leo.fixtures import DATA_DIR, load_fixture
from leo.ingest.ingest import (
    ATTACHMENT,
    ATTACHMENT_LINK,
    EML,
    FILE,
    ingest,
    ingest_dataset,
    ingest_folder,
)

PDF = b"%PDF-1.4\n\x00\xff binary"
MD = "# Agreement\n\nFee: $339/month — café\n".encode()


@pytest.fixture
def repo(session):
    return Repo(session, upsert_tenant(session, "ingest-test", "Ingest test"))


def _docs(repo: Repo) -> list[SourceDocument]:
    repo.session.flush()
    q = select(SourceDocument).where(SourceDocument.tenant_id == repo.tenant_id)
    return list(repo.session.scalars(q.order_by(SourceDocument.path)))


def _links(repo: Repo) -> set[tuple]:
    q = select(DocumentLink.document_id, DocumentLink.linked_document_id, DocumentLink.link_type)
    return set(repo.session.execute(q.where(DocumentLink.tenant_id == repo.tenant_id)).all())


def _email(subject: str = "Docs", message_id: str = "<a@x.example>", pdf: bool = True) -> bytes:
    inner = EmailMessage()
    inner["Subject"] = "Original"
    inner.set_content("The forwarded original.\n")
    msg = EmailMessage()
    msg["From"] = "Sam <sam@brightwater.example>"
    msg["To"] = "maya@brightwater.example"
    msg["Subject"] = subject
    msg["Date"] = "Thu, 15 Oct 2026 08:41:00 -0700"
    msg["Message-ID"] = message_id
    msg.set_content("See attached.\n")
    if pdf:
        msg.add_attachment(PDF, maintype="application", subtype="pdf", filename="order.pdf")
    msg.add_attachment(MD, maintype="text", subtype="markdown", filename="terms.md")
    msg.add_attachment(inner)
    return msg.as_bytes()


def test_dataset_email_row(repo, raw_store):
    raw = (DATA_DIR / "inbox/01_lumenpost_price_change_notice.eml").read_bytes()
    r = ingest(repo, raw_store, raw, "01_lumenpost_price_change_notice.eml", path="inbox/01.eml")
    (doc,) = _docs(repo)
    assert (r.document_id, r.natural_key, r.created) == (doc.id, keys.eml_hash(raw), True)
    assert doc.source_kind == EML
    assert doc.path == "inbox/01.eml"
    assert doc.subject == "Important update to your Lumenpost plan pricing"
    assert doc.message_id and doc.received_at and doc.received_at.tzinfo
    assert raw_store.read(doc.storage_path) == raw
    assert doc.content_sha256 == hashlib.sha256(raw).hexdigest()
    assert r.attachment_ids == []


def test_contract_file_row(repo, raw_store):
    raw = (DATA_DIR / "contracts/kestrel_master_services_agreement.md").read_bytes()
    r = ingest(repo, raw_store, raw, "kestrel_master_services_agreement.md")
    (doc,) = _docs(repo)
    assert r.natural_key == keys.file(raw)
    assert (doc.source_kind, doc.path) == (FILE, "uploads/kestrel_master_services_agreement.md")
    assert doc.message_id is None and doc.received_at is None
    assert doc.storage_path.endswith(".md")


def test_same_bytes_twice_is_one_row_and_first_path_wins(repo, raw_store):
    raw = _email()
    first = ingest(repo, raw_store, raw, "a.eml", path="inbox/a.eml")
    before = _docs(repo)
    again = ingest(repo, raw_store, raw, "copy.eml", path="inbox/copy.eml")
    assert again.document_id == first.document_id and not again.created
    assert again.attachment_ids == first.attachment_ids
    after = _docs(repo)
    assert [d.id for d in after] == [d.id for d in before]
    assert after[0].path == "inbox/a.eml"


def test_message_id_is_not_identity(repo, raw_store):
    a = ingest(repo, raw_store, _email(message_id="<same@x>"), "a.eml")
    b = ingest(repo, raw_store, _email(message_id="<same@x>", subject="Docs again"), "b.eml")
    assert a.document_id != b.document_id
    c = ingest(repo, raw_store, _email(message_id="<other@x>"), "c.eml")
    assert c.document_id not in (a.document_id, b.document_id)


def test_attachments_are_rows_stored_once_and_linked(repo, raw_store):
    first = ingest(repo, raw_store, _email(), "a.eml", path="inbox/a.eml")
    assert len(first.attachment_ids) == 3
    docs = {d.id: d for d in _docs(repo)}
    pdf, md, fwd = (docs[i] for i in first.attachment_ids)
    assert {d.source_kind for d in (pdf, md, fwd)} == {ATTACHMENT}
    assert pdf.path == "inbox/a.eml#attachments/order.pdf"
    assert fwd.path == "inbox/a.eml#attachments/attached-3.eml"
    assert md.natural_key == keys.file(MD)  # the same key as terms.md uploaded on its own
    assert raw_store.read(pdf.storage_path) == PDF
    assert {d.parent_document_id for d in (pdf, md, fwd)} == {first.document_id}

    second = ingest(repo, raw_store, _email(subject="Again"), "b.eml", path="inbox/b.eml")
    assert second.attachment_ids[0] == pdf.id  # the PDF row is reused
    repo.session.refresh(pdf)
    assert pdf.path == "inbox/a.eml#attachments/order.pdf"  # first carrier wins
    assert pdf.parent_document_id == first.document_id
    links = _links(repo)
    assert (first.document_id, pdf.id, ATTACHMENT_LINK) in links
    assert (second.document_id, pdf.id, ATTACHMENT_LINK) in links
    assert len(list(raw_store.root.rglob("*.pdf"))) == 1


def test_attachment_matching_an_uploaded_file_reuses_its_row(repo, raw_store):
    upload = ingest(repo, raw_store, MD, "terms.md", path="contracts/terms.md")
    r = ingest(repo, raw_store, _email(pdf=False), "a.eml")
    assert r.attachment_ids[0] == upload.document_id
    doc = next(d for d in _docs(repo) if d.id == upload.document_id)
    assert (doc.source_kind, doc.path, doc.parent_document_id) == (FILE, "contracts/terms.md", None)
    assert (r.document_id, upload.document_id, ATTACHMENT_LINK) in _links(repo)


def test_rerun_with_attachments_adds_nothing(repo, raw_store):
    raw = _email()  # built once: EmailMessage picks a random MIME boundary per build
    ingest(repo, raw_store, raw, "a.eml")
    n_docs, n_links = len(_docs(repo)), len(_links(repo))
    ingest(repo, raw_store, raw, "a.eml")
    assert (len(_docs(repo)), len(_links(repo))) == (n_docs, n_links)


@pytest.mark.parametrize("raw, name", [(b"", "a.eml"), (b"x", ""), (b"x", "../")])
def test_bad_input_raises(repo, raw_store, raw, name):
    with pytest.raises(ValueError):
        ingest(repo, raw_store, raw, name)


def test_filename_directories_are_dropped(repo, raw_store):
    ingest(repo, raw_store, b"hello", "../../etc/notes.txt")
    (doc,) = _docs(repo)
    assert (doc.filename, doc.path) == ("notes.txt", "uploads/notes.txt")


# --- Folder loaders (A3.4) -------------------------------------------------------------------


def test_synthetic_dataset_loads_one_row_per_distinct_document(repo, raw_store):
    fixture = load_fixture()
    results = ingest_dataset(repo, raw_store, DATA_DIR)
    assert len(results) == len(fixture.documents)  # every file is read
    docs = _docs(repo)
    assert len(docs) == len(fixture.documents) - 1  # D1's re-synced copy shares a row
    fixture_paths = {d.path for d in fixture.documents}
    assert {d.path for d in docs} == fixture_paths - {
        "inbox/07_kestrel_invoice_2026_10_resynced.eml"  # sorted order: 06 is written first
    }
    for d in docs:
        raw = raw_store.read(d.storage_path)
        assert hashlib.sha256(raw).hexdigest() == d.content_sha256
        assert raw == (DATA_DIR / d.path).read_bytes()
        assert d.source_kind == (EML if d.path.startswith("inbox/") else FILE)


def test_dataset_rerun_adds_nothing(repo, raw_store):
    ingest_dataset(repo, raw_store, DATA_DIR)
    before = [(d.id, d.path) for d in _docs(repo)]
    again = ingest_dataset(repo, raw_store, DATA_DIR)
    assert not any(r.created for r in again)
    assert [(d.id, d.path) for d in _docs(repo)] == before


def test_holdout_dataset_loads(repo, raw_store):
    assert len(ingest_dataset(repo, raw_store, HOLDOUT_DIR)) == 5


def test_folder_skips_dotfiles_readmes_and_subfolders(repo, raw_store, tmp_path):
    folder = tmp_path / "data" / "contracts"
    (folder / "nested").mkdir(parents=True)
    (folder / "b.md").write_bytes(b"b")
    (folder / "a.md").write_bytes(b"a")
    (folder / ".DS_Store").write_bytes(b"junk")
    (folder / "README.md").write_bytes(b"readme")
    (folder / "nested" / "c.md").write_bytes(b"c")
    results = ingest_folder(repo, raw_store, tmp_path / "data", "contracts")
    assert [r.natural_key for r in results] == [keys.file(b"a"), keys.file(b"b")]
    assert [d.path for d in _docs(repo)] == ["contracts/a.md", "contracts/b.md"]


def test_nul_in_headers_and_filenames_inserts(repo, raw_store):
    raw = (
        b"Subject: hi\x00there\r\nMessage-ID: <a\x00b@x>\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nBody.\r\n"
        b"--b\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="ev\x00il.pdf"\r\n\r\n%PDF\r\n'
        b"--b--\r\n"
    )
    r = ingest(repo, raw_store, raw, "nul.eml")
    docs = {d.id: d for d in _docs(repo)}
    assert docs[r.document_id].subject == "hithere"
    assert docs[r.attachment_ids[0]].path == "uploads/nul.eml#attachments/evil.pdf"
