"""Findings, documents and the approval queue (ROADMAP C2, served from `FixtureStore`)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from leo.api.schemas import (
    ActionEdit,
    ActionOut,
    Dashboard,
    DocumentOut,
    FindingDetail,
    FindingSummary,
)
from leo.api.store import FixtureStore, InvalidTransition, NotFound, get_store

router = APIRouter(prefix="/api")
Store = Annotated[FixtureStore, Depends(get_store)]


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
    """Only the dataset's documents, looked up by exact path."""
    try:
        return store.document(path)
    except NotFound:
        raise _not_found("document", path) from None


@router.get("/actions")
def actions(store: Store) -> list[ActionOut]:
    return store.queue()


@router.get("/outbox")
def outbox(store: Store) -> list[ActionOut]:
    """Sent drafts. Sending is simulated: nothing leaves LEO."""
    return store.outbox


def _act(store: FixtureStore, action_id: str, fn) -> ActionOut:
    try:
        return fn()
    except NotFound:
        raise _not_found("action", action_id) from None
    except InvalidTransition as e:
        raise HTTPException(status_code=409, detail=str(e)) from None


@router.patch("/actions/{action_id}")
def edit_action(action_id: str, edit: ActionEdit, store: Store) -> ActionOut:
    return _act(store, action_id, lambda: store.edit(action_id, edit))


@router.post("/actions/{action_id}/approve")
def approve_action(action_id: str, store: Store) -> ActionOut:
    return _act(store, action_id, lambda: store.approve(action_id))


@router.post("/actions/{action_id}/reject")
def reject_action(action_id: str, store: Store) -> ActionOut:
    return _act(store, action_id, lambda: store.reject(action_id))


@router.post("/actions/{action_id}/restore")
def restore_action(action_id: str, store: Store) -> ActionOut:
    """Undo a reject: back to draft."""
    return _act(store, action_id, lambda: store.restore(action_id))


@router.post("/actions/{action_id}/send")
def send_action(action_id: str, store: Store) -> ActionOut:
    return _act(store, action_id, lambda: store.send(action_id))
