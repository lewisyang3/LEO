"""`ingest()`: one raw file in, one `source_document` row (plus its attachments) out (ROADMAP A3).

The upload endpoint (C3), the folder loaders and the seed all come through here, so a document
is written one way. The raw bytes go to the `RawStore` and the row holds the reference.

- Keys come from the bytes (`leo.db.keys.document_key`), never from `Message-ID` or other
  headers, which are stored as attributes. The same bytes ingested twice give the same row.
- `path` is the display path. Folder loads pass the dataset-relative path (`inbox/01_….eml`),
  uploads default to `uploads/{filename}`. It is insert-only, so the first path wins (D1).
- Each attachment is its own document, keyed on its bytes like the same file on its own, and
  stored once. `parent_document_id` is the first email that carried it, and every carrier gets
  a `document_link` of type `attachment` (email -> attachment).
- Never commits: the caller owns the transaction. The file is stored before the row is
  written, so a rollback can leave an unreferenced file, never a row without its file.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from uuid import UUID

from sqlalchemy import select

from leo.db import keys
from leo.db.models import SourceDocument
from leo.db.repo import Repo
from leo.ingest.eml import Attachment, parse_eml, safe_filename
from leo.ingest.store import RawStore
from leo.worker import queue

EML = "eml"
FILE = "file"
ATTACHMENT = "attachment"
ATTACHMENT_LINK = "attachment"
UPLOADS = "uploads"
DATASET_FOLDERS = ("inbox", "contracts")
SKIPPED_FILES = {"readme.md"}


@dataclass(frozen=True)
class IngestResult:
    document_id: UUID
    natural_key: str
    created: bool  # False when these bytes were already ingested for this tenant
    attachment_ids: list[UUID] = field(default_factory=list)


def _exists(repo: Repo, natural_key: str) -> bool:
    return (
        repo.session.scalar(
            select(SourceDocument.id).where(
                SourceDocument.tenant_id == repo.tenant_id,
                SourceDocument.natural_key == natural_key,
            )
        )
        is not None
    )


def _stored(store: RawStore, raw: bytes, filename: str) -> dict:
    return {
        "storage_path": store.put(raw, PurePath(filename).suffix),
        "filename": filename,
        "content_sha256": hashlib.sha256(raw).hexdigest(),
    }


def ingest(
    repo: Repo,
    store: RawStore,
    raw: bytes,
    filename: str,
    *,
    path: str | None = None,
    enqueue: bool = True,
) -> IngestResult:
    """Store one file and upsert its document row, and its attachments' rows if it's an email.

    `filename` decides the kind (`.eml` is an email, anything else a file); only its bare name
    is kept. `path` defaults to `uploads/{filename}`.

    With `enqueue`, each document (attachments too) gets a `classify` job for the worker (A4).
    A document that already has one keeps it, whatever its status, so re-ingesting requeues
    nothing. The seed passes `enqueue=False`: it writes the answer key's results itself.
    """
    name = safe_filename(filename)
    if name is None:
        raise ValueError(f"not a usable filename: {filename!r}")
    if not raw:
        raise ValueError(f"empty file: {name}")
    path = path or f"{UPLOADS}/{name}"
    key = keys.document_key(name, raw)
    created = not _exists(repo, key)
    cols = {"path": path, **_stored(store, raw, name)}

    if not keys.is_eml(name):
        doc_id = repo.upsert_document(key, source_kind=FILE, **cols)
        if enqueue:
            _enqueue(repo, doc_id, key)
        return IngestResult(doc_id, key, created)

    email = parse_eml(raw)
    doc_id = repo.upsert_document(
        key,
        source_kind=EML,
        sender=email.sender,
        recipient=email.recipient,
        subject=email.subject,
        message_id=email.message_id,
        received_at=email.received_at,
        **cols,
    )
    if enqueue:
        _enqueue(repo, doc_id, key)
    attachment_ids = [_attachment(repo, store, doc_id, path, a, enqueue) for a in email.attachments]
    return IngestResult(doc_id, key, created, attachment_ids)


def _enqueue(repo: Repo, document_id: UUID, doc_key: str) -> None:
    queue.enqueue(repo.session, repo.tenant_id, document_id, doc_key, queue.CLASSIFY)


def _attachment(
    repo: Repo, store: RawStore, email_id: UUID, email_path: str, a: Attachment, enqueue: bool
) -> UUID:
    """Upsert one attachment and link it from its email. Bytes already ingested (another
    email's attachment, or the same file uploaded on its own) reuse that row unchanged."""
    att_key = keys.document_key(a.filename, a.raw)
    att_id = repo.upsert_document(
        att_key,
        source_kind=ATTACHMENT,
        path=f"{email_path}#attachments/{a.filename}",
        parent_document_id=email_id,
        **_stored(store, a.raw, a.filename),
    )
    repo.link_documents(email_id, att_id, ATTACHMENT_LINK)
    if enqueue:
        _enqueue(repo, att_id, att_key)
    return att_id


def _loadable(p: Path) -> bool:
    return p.is_file() and not p.name.startswith(".") and p.name.lower() not in SKIPPED_FILES


def ingest_folder(repo: Repo, store: RawStore, root: Path, folder: str) -> list[IngestResult]:
    """Ingest every file directly in `root/folder` (not subfolders), sorted by name so a
    rerun writes in the same order and the first copy of repeated bytes keeps its path.
    Paths are stored as `{folder}/{name}`, relative to `root`. Dotfiles and READMEs are
    skipped. The contracts loader is `ingest_folder(repo, store, root, "contracts")`."""
    files = sorted(
        (p for p in (Path(root) / folder).iterdir() if _loadable(p)), key=lambda p: p.name
    )
    return [ingest(repo, store, p.read_bytes(), p.name, path=f"{folder}/{p.name}") for p in files]


def ingest_dataset(repo: Repo, store: RawStore, root: Path) -> list[IngestResult]:
    """Ingest a dataset laid out like `data/synthetic/`: `inbox/` then `contracts/`."""
    return [r for folder in DATASET_FOLDERS for r in ingest_folder(repo, store, root, folder)]
