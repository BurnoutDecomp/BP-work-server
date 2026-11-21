from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query, status
from fastapi.responses import FileResponse, JSONResponse

from bp_work_server import bytematch_jobs
from bp_work_server.dependencies import get_store, require_admin_worker
from bp_work_server.models import (
    BytematchMeasureRequest,
    ImportResponse,
    SyncRequest,
    SyncResponse,
    SourceStatusRequest,
    WorkerCreateRequest,
    WorkerListResponse,
    WorkerResponse,
)
from bp_work_server.services.dashboard import invalidate_dashboard_cache
from bp_work_server.store import WorkStore

router = APIRouter(prefix="/admin")
log = logging.getLogger(__name__)


@router.post("/source-status")
def reconcile_source_status(
    req: SourceStatusRequest,
    request: Request,
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> dict:
    from bp_work_server import source_status

    try:
        result = source_status.apply(store, req.model_dump())
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    invalidate_dashboard_cache(request)
    return result


@router.post("/workers", response_model=WorkerResponse, status_code=status.HTTP_201_CREATED)
def create_worker(
    req: WorkerCreateRequest,
    request: Request,
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> WorkerResponse:
    result = store.create_worker(
        req.username,
        is_admin=req.is_admin,
        github_username=req.github_username,
        is_service=req.is_service,
    )
    invalidate_dashboard_cache(request)
    log.info(
        "admin created worker username=%s admin=%s service=%s",
        req.username,
        req.is_admin,
        req.is_service,
    )
    return WorkerResponse(**result)


@router.get("/workers", response_model=WorkerListResponse)
def list_workers(
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> WorkerListResponse:
    return WorkerListResponse(workers=store.list_workers())


@router.delete("/workers/{token}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_worker(
    token: str,
    request: Request,
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> Response:
    if not store.revoke_worker(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown worker token")
    invalidate_dashboard_cache(request)
    log.info("admin revoked worker token_suffix=%s", token[-6:])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/import", response_model=ImportResponse)
def import_workflow(
    request: Request,
    workflow_root: str = Query(..., description="Path to BP-Decomp_Workflow"),
    reset: bool = Query(False),
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> dict[str, int]:
    try:
        result = store.import_workflow(workflow_root, reset=reset)
        invalidate_dashboard_cache(request)
        log.info("admin import workflow_root=%s reset=%s", workflow_root, reset)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/sync", response_model=SyncResponse)
def sync_workflow(
    req: SyncRequest,
    request: Request,
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> dict:
    try:
        import bp_work_server.api as api

        options = {"branch": req.branch, "reset": req.reset}
        if req.metadata_only:
            options["metadata_only"] = True
        result = api.sync_workflow_repo(store, **options)
        # Bring the attribution clone to the same tip the workflow just advanced to,
        # so contribution numbers reflect the synced revision (not a TTL-stale clone).
        decomp = getattr(request.app.state, "decomp", None)
        if decomp is not None:
            decomp.force_refresh()
        invalidate_dashboard_cache(request)
        log.info("admin sync branch=%s reset=%s", req.branch, req.reset)
        return result
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# --- byte matching: CI asks the server to measure a native commit (bytematch_jobs) ---------
@router.post("/bytematch/measure")
def bytematch_measure(req: BytematchMeasureRequest, admin: str = Depends(require_admin_worker)) -> JSONResponse:
    """Queue a measurement of one x360 commit; answers its status (202 when newly queued)."""
    try:
        current, queued = bytematch_jobs.request(req.sha, admin)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if queued:
        log.info("admin bytematch measure sha=%s by=%s", current["sha"], admin)
    return JSONResponse(current, status_code=status.HTTP_202_ACCEPTED if queued else status.HTTP_200_OK)


@router.get("/bytematch/measure/{sha}")
def bytematch_measure_status(sha: str, _admin: str = Depends(require_admin_worker)) -> dict:
    try:
        current = bytematch_jobs.status(sha)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no measurement of that commit")
    return current


@router.get("/bytematch/measure/{sha}/bundle")
def bytematch_measure_bundle(sha: str, _admin: str = Depends(require_admin_worker)) -> FileResponse:
    try:
        path = bytematch_jobs.bundle(sha)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no finished measurement of that commit")
    return FileResponse(path, media_type="application/gzip", filename=f"bytematch-{sha[:12]}.json.gz")
