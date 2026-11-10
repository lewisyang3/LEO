"""Findings, documents, the approval queue, upload, stats (ROADMAP C2, C3), CSV export (C4) and
vendors (C5), from Postgres."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from leo.api.schemas import (
    ActionEdit,
    ActionOut,
    CorrectionOut,
    Dashboard,
    DocumentOut,
    FindingDetail,
    FindingSummary,
    InvoiceFactCorrection,
    Stats,
    TermCorrection,
    UploadOut,
    VendorDetail,
    VendorSummary,
)
from leo.api.store import DbStore, InvalidCorrection, InvalidTransition, NotFound
from leo.clock import get_clock
from leo.config import get_settings
from leo.db.models import Job, ModelUsage, SourceDocument, StageExecution, Tenant
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


@router.get("/vendors")
def vendors(store: Store) -> list[VendorSummary]:
    """By name, with counts of terms, transactions and open findings."""
    return store.vendors()


@router.get("/vendors/{vendor_id}")
def vendor(vendor_id: str, store: Store) -> VendorDetail:
    """Findings, price history, terms and matched transactions (C5)."""
    try:
        return store.vendor(vendor_id)
    except NotFound:
        raise _not_found("vendor", vendor_id) from None


@router.get("/documents")
def document(path: str, store: Store) -> DocumentOut:
    """The tenant's stored documents, looked up by exact path."""
    try:
        return store.document(path)
    except NotFound:
        raise _not_found("document", path) from None


@router.post("/documents")
def upload_document(file: UploadFile, store: Store) -> UploadOut:
    """Store one file (`ingest`, A3) and queue it. The worker (A4) processes it."""
    raw = file.file.read()
    try:
        return store.upload(file.filename or "", raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


@router.get("/stats")
def stats(store: Store) -> Stats:
    return store.stats()


@router.get("/analytics/jobs/{job_id}")
def analytics_job(job_id: str, store: Store) -> dict:
    """Attempts and individual provider calls for a single stage job."""
    from uuid import UUID

    try:
        uid = UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid job UUID") from None
    s = store.session
    job = s.scalar(select(Job).where(Job.id == uid, Job.tenant_id == store.tenant_id))
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    executions = s.scalars(
        select(StageExecution)
        .where(StageExecution.job_id == job.id, StageExecution.tenant_id == store.tenant_id)
        .order_by(StageExecution.started_at, StageExecution.id)
    ).all()
    items = []
    for execution in executions:
        usages = s.scalars(
            select(ModelUsage)
            .where(
                ModelUsage.stage_execution_id == execution.id,
                ModelUsage.tenant_id == store.tenant_id,
            )
            .order_by(ModelUsage.attempt_number)
        ).all()
        items.append(
            {
                "stage_execution_id": str(execution.id),
                "status": execution.status,
                "stop_reason": execution.stop_reason,
                "started_at": execution.started_at.isoformat(),
                "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
                "api_calls": [
                    {
                        "provider": usage.provider,
                        "model": usage.model,
                        "attempt_number": usage.attempt_number,
                        "status": usage.status,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                        "cache_read_input_tokens": usage.cache_read_input_tokens,
                        "cost_usd": str(usage.cost_usd) if usage.cost_usd is not None else None,
                    }
                    for usage in usages
                ],
            }
        )
    return {
        "job_id": str(job.id),
        "document_id": str(job.document_id) if job.document_id else None,
        "stage": job.stage,
        "status": job.status,
        "executions": items,
    }


@router.get("/analytics/summary")
def analytics_summary(store: Store) -> dict:
    """Tenant-scoped B6 funnel, per-provider spend, and stage outcome counts.

    Reached counts distinct jobs, not retry attempts. API counts include failed
    outbound calls; costs are partial if provider rates or usage are unavailable.
    """
    s = store.session
    tenant = store.tenant_id
    ingested = (
        s.scalar(select(func.count(SourceDocument.id)).where(SourceDocument.tenant_id == tenant))
        or 0
    )
    mail = (
        s.scalar(
            select(func.count(SourceDocument.id)).where(
                SourceDocument.tenant_id == tenant,
                SourceDocument.source_kind.in_(("eml", "gmail")),
            )
        )
        or 0
    )
    reached = s.execute(
        select(StageExecution.stage, func.count(distinct(StageExecution.job_id)))
        .where(StageExecution.tenant_id == tenant)
        .group_by(StageExecution.stage)
    ).all()
    outcomes = s.execute(
        select(StageExecution.stage, StageExecution.status, func.count(StageExecution.id))
        .where(StageExecution.tenant_id == tenant)
        .group_by(StageExecution.stage, StageExecution.status)
    ).all()
    stops = s.execute(
        select(Job.stage, Job.status, func.count(Job.id))
        .where(Job.tenant_id == tenant, Job.status.in_(("failed", "parked")))
        .group_by(Job.stage, Job.status)
    ).all()
    api = s.execute(
        select(
            ModelUsage.provider,
            func.count(ModelUsage.id),
            func.coalesce(func.sum(ModelUsage.input_tokens), 0),
            func.coalesce(func.sum(ModelUsage.output_tokens), 0),
            func.coalesce(func.sum(ModelUsage.cache_creation_input_tokens), 0),
            func.coalesce(func.sum(ModelUsage.cache_read_input_tokens), 0),
            func.coalesce(func.sum(ModelUsage.cost_usd), 0),
            func.count(ModelUsage.id).filter(ModelUsage.cost_usd.is_(None)),
        )
        .where(ModelUsage.tenant_id == tenant)
        .group_by(ModelUsage.provider)
    ).all()
    return {
        "documents_ingested": ingested,
        "emails_ingested": mail,
        "reached_by_stage": dict(reached),
        "attempt_outcomes": [
            {"stage": stage, "status": status, "count": count} for stage, status, count in outcomes
        ],
        "terminal_failures": [
            {"stage": stage, "status": status, "count": count} for stage, status, count in stops
        ],
        "providers": [
            {
                "provider": provider,
                "api_attempts": count,
                "input_tokens": int(inp),
                "output_tokens": int(out),
                "cache_creation_input_tokens": int(cw),
                "cache_read_input_tokens": int(cr),
                "known_cost_usd": str(cost),
                "unpriced_attempts": missing,
            }
            for provider, count, inp, out, cw, cr, cost, missing in api
        ],
    }


def _csv(name: str, body: str, store: DbStore) -> Response:
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="leo-{name}-{store.today}.csv"'},
    )


@router.get("/export/terms.csv")
def export_terms(store: Store) -> Response:
    """The term record: every stored term, in every status."""
    return _csv("terms", store.export_terms(), store)


@router.get("/export/findings.csv")
def export_findings(store: Store) -> Response:
    """The findings, in the dashboard's order."""
    return _csv("findings", store.export_findings(), store)


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


# --- Corrections (feedback loop, Phase 4) ----------------------------------------------------


def _fix(what: str, item_id: str, fn) -> CorrectionOut:
    try:
        return fn()
    except NotFound:
        raise _not_found(what, item_id) from None
    except InvalidTransition as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except InvalidCorrection as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


@router.patch("/terms/{term_id}")
def correct_term(term_id: str, edit: TermCorrection, store: Store) -> CorrectionOut:
    """A reviewer's correction: re-extraction never overwrites it, and it feeds the prompts."""
    return _fix("term", term_id, lambda: store.correct_term(term_id, edit))


@router.post("/terms/{term_id}/reject")
def reject_term(term_id: str, store: Store) -> CorrectionOut:
    """Not a term: left out of findings from the next reconcile."""
    return _fix("term", term_id, lambda: store.reject_item("term", term_id))


@router.patch("/invoice-facts/{fact_id}")
def correct_invoice_fact(fact_id: str, edit: InvoiceFactCorrection, store: Store) -> CorrectionOut:
    return _fix("invoice fact", fact_id, lambda: store.correct_invoice_fact(fact_id, edit))


@router.post("/invoice-facts/{fact_id}/reject")
def reject_invoice_fact(fact_id: str, store: Store) -> CorrectionOut:
    return _fix("invoice fact", fact_id, lambda: store.reject_item("invoice_fact", fact_id))
