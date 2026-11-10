"""Findings, documents, the approval queue, upload and stats (ROADMAP C2, C3), from Postgres."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from leo.api.schemas import (
    ActionEdit,
    ActionOut,
    Dashboard,
    DocumentOut,
    FindingDetail,
    FindingSummary,
    Stats,
    UploadOut,
)
from leo.api.store import DbStore, InvalidTransition, NotFound
from leo.clock import get_clock
from leo.config import get_settings
from leo.db.models import Tenant
from leo.db.session import get_sessionmaker
from leo.ingest.store import RawStore

router = APIRouter(prefix="/api")


def get_session() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def get_store(session: Annotated[Session, Depends(get_session)]) -> DbStore:
    """The configured tenant (`LEO_TENANT`). 503 until `make seed` has created it."""
    settings = get_settings()
    tenant_id = session.scalar(select(Tenant.id).where(Tenant.slug == settings.leo_tenant))
    if tenant_id is None:
        raise HTTPException(
            status_code=503, detail=f"no tenant {settings.leo_tenant!r}: run `make seed`"
        )
    return DbStore(
        session,
        tenant_id,
        today=get_clock(settings).today(),
        raw=RawStore(settings.leo_raw_dir),
        approver=settings.leo_approver,
    )


Store = Annotated[DbStore, Depends(get_store)]


def _not_found(what: str, key: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"no {what} {key!r}")


@router.get("/dashboard")
def dashboard(store: Store) -> Dashboard:
    return store.dashboard()


@router.get("/findings")
def findings(store: Store) -> list[FindingSummary]:
    """Sorted by days left (no deadline last), then dollars at stake."""
    return store.summaries()


@router.get("/findings/{finding_id}")
def finding(finding_id: str, store: Store) -> FindingDetail:
    try:
        return store.detail(finding_id)
    except NotFound:
        raise _not_found("finding", finding_id) from None


@router.get("/documents")
def document(path: str, store: Store) -> DocumentOut:
    """The tenant's stored documents, looked up by exact path."""
    try:
        return store.document(path)
    except NotFound:
        raise _not_found("document", path) from None


@router.post("/documents")
def upload_document(file: UploadFile, store: Store) -> UploadOut:
    """Store one file (`ingest`, A3). Processing it waits for the worker (A4, 2.1)."""
    raw = file.file.read()
    try:
        return store.upload(file.filename or "", raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


@router.get("/stats")
def stats(store: Store) -> Stats:
    return store.stats()


@router.get("/actions")
def actions(store: Store) -> list[ActionOut]:
    return store.queue()


@router.get("/outbox")
def outbox(store: Store) -> list[ActionOut]:
    """Sent drafts. Sending is simulated: nothing leaves LEO."""
    return store.outbox()


def _act(action_id: str, fn) -> ActionOut:
    try:
        return fn()
    except NotFound:
        raise _not_found("action", action_id) from None
    except InvalidTransition as e:
        raise HTTPException(status_code=409, detail=str(e)) from None


@router.patch("/actions/{action_id}")
def edit_action(action_id: str, edit: ActionEdit, store: Store) -> ActionOut:
    return _act(action_id, lambda: store.edit(action_id, edit))


@router.post("/actions/{action_id}/approve")
def approve_action(action_id: str, store: Store) -> ActionOut:
    return _act(action_id, lambda: store.approve(action_id))


@router.post("/actions/{action_id}/reject")
def reject_action(action_id: str, store: Store) -> ActionOut:
    return _act(action_id, lambda: store.reject(action_id))


@router.post("/actions/{action_id}/restore")
def restore_action(action_id: str, store: Store) -> ActionOut:
    """Undo a reject: back to draft."""
    return _act(action_id, lambda: store.restore(action_id))


@router.post("/actions/{action_id}/send")
def send_action(action_id: str, store: Store) -> ActionOut:
    return _act(action_id, lambda: store.send(action_id))
