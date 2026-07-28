from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query, status

from bp_work_server.dependencies import get_store, require_admin_worker
from bp_work_server.models import (
    ImportResponse,
    SyncRequest,
    SyncResponse,
    WorkerCreateRequest,
    WorkerListResponse,
    WorkerResponse,
)
from bp_work_server.services.dashboard import invalidate_dashboard_cache
from bp_work_server.store import WorkStore

router = APIRouter(prefix="/admin")
log = logging.getLogger(__name__)


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

        result = api.sync_workflow_repo(store, branch=req.branch, reset=req.reset)
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
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
