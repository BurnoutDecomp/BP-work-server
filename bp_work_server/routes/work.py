from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from bp_work_server.dependencies import get_store, worker_identity
from bp_work_server.models import (
    AgentRequest,
    BlockRequest,
    ClaimNextRequest,
    ClaimNextResponse,
    ClaimRequest,
    ClaimResponse,
    HeartbeatRequest,
    NextResponse,
    SnapshotResponse,
    StatusUpdateRequest,
    ReviewRequest,
)
from bp_work_server.services.dashboard import invalidate_dashboard_cache
from bp_work_server.store import WorkStore

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/export/status")
def export_status(store: WorkStore = Depends(get_store)) -> dict:
    return store.export_status()


@router.get("/snapshot", response_model=SnapshotResponse)
def snapshot(
    include_tus: bool = Query(True),
    store: WorkStore = Depends(get_store),
) -> SnapshotResponse:
    active_goal, counts, tus = store.snapshot(include_tus=include_tus)
    return SnapshotResponse(active_goal=active_goal, counts=counts, tus=tus)


@router.get("/next", response_model=NextResponse)
def next_tus(
    n: int = Query(1, ge=1, le=100),
    goal: str | None = Query(None),
    store: WorkStore = Depends(get_store),
) -> NextResponse:
    active_goal, items = store.next_tus(n=n, goal=goal)
    return NextResponse(active_goal=active_goal, count=len(items), items=items)


@router.post("/claims", response_model=ClaimResponse, status_code=status.HTTP_201_CREATED)
def claim(
    req: ClaimRequest,
    response: Response,
    request: Request,
    store: WorkStore = Depends(get_store),
    identity: str | None = Depends(worker_identity),
):
    try:
        result = store.claim(req.tu, identity or req.agent, req.lease_seconds, force=req.force)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    invalidate_dashboard_cache(request)
    if not result.claimed:
        response.status_code = status.HTTP_409_CONFLICT
    log.info("claim tu=%s actor=%s claimed=%s", req.tu, identity or req.agent, result.claimed)
    return result


@router.post("/claims/next", response_model=ClaimNextResponse)
def claim_next(
    req: ClaimNextRequest,
    request: Request,
    store: WorkStore = Depends(get_store),
    identity: str | None = Depends(worker_identity),
) -> ClaimNextResponse:
    active_goal, claimed = store.claim_next(
        identity or req.agent, n=req.n, lease_seconds=req.lease_seconds, goal=req.goal
    )
    invalidate_dashboard_cache(request)
    log.info("claim_next actor=%s requested=%s claimed=%s", identity or req.agent, req.n, len(claimed))
    return ClaimNextResponse(active_goal=active_goal, count=len(claimed), claimed=claimed)


@router.post("/claims/{tu_id:path}/heartbeat", response_model=ClaimResponse)
def heartbeat(
    tu_id: str,
    req: HeartbeatRequest,
    response: Response,
    request: Request,
    store: WorkStore = Depends(get_store),
    identity: str | None = Depends(worker_identity),
):
    try:
        result = store.heartbeat(tu_id, identity or req.agent, req.lease_seconds)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    invalidate_dashboard_cache(request)
    if not result.claimed:
        response.status_code = status.HTTP_409_CONFLICT
    return result


@router.delete("/claims/{tu_id:path}", status_code=status.HTTP_204_NO_CONTENT)
def release(
    tu_id: str,
    req: AgentRequest,
    request: Request,
    store: WorkStore = Depends(get_store),
    identity: str | None = Depends(worker_identity),
) -> Response:
    try:
        store.release(tu_id, identity or req.agent)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    invalidate_dashboard_cache(request)
    log.info("release tu=%s actor=%s", tu_id, identity or req.agent)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tu/{tu_id:path}/compiled", status_code=status.HTTP_204_NO_CONTENT)
def compiled(
    tu_id: str,
    req: StatusUpdateRequest,
    request: Request,
    store: WorkStore = Depends(get_store),
    identity: str | None = Depends(worker_identity),
) -> Response:
    try:
        store.mark_compiled(tu_id, identity or req.agent, notes=req.notes, commit=req.commit)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    invalidate_dashboard_cache(request)
    log.info("compiled tu=%s actor=%s", tu_id, identity or req.agent)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tu/{tu_id:path}/review", status_code=status.HTTP_204_NO_CONTENT)
def review(
    tu_id: str,
    req: ReviewRequest,
    request: Request,
    store: WorkStore = Depends(get_store),
    identity: str | None = Depends(worker_identity),
) -> Response:
    try:
        store.review(tu_id, identity or req.agent, req.verdict, notes=req.notes, commit=req.commit)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    invalidate_dashboard_cache(request)
    log.info("review tu=%s actor=%s verdict=%s", tu_id, identity or req.agent, req.verdict)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tu/{tu_id:path}/block", status_code=status.HTTP_204_NO_CONTENT)
def block(
    tu_id: str,
    req: BlockRequest,
    request: Request,
    store: WorkStore = Depends(get_store),
    identity: str | None = Depends(worker_identity),
) -> Response:
    try:
        store.block(tu_id, identity or req.agent, req.reason)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    invalidate_dashboard_cache(request)
    log.info("block tu=%s actor=%s", tu_id, identity or req.agent)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tu/{tu_id:path}/unblock", status_code=status.HTTP_204_NO_CONTENT)
def unblock(
    tu_id: str,
    req: AgentRequest,
    request: Request,
    store: WorkStore = Depends(get_store),
    identity: str | None = Depends(worker_identity),
) -> Response:
    try:
        store.unblock(tu_id, identity or req.agent)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    invalidate_dashboard_cache(request)
    log.info("unblock tu=%s actor=%s", tu_id, identity or req.agent)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tu/{tu_id:path}/reset", status_code=status.HTTP_204_NO_CONTENT)
def reset_tu(
    tu_id: str,
    req: StatusUpdateRequest,
    request: Request,
    store: WorkStore = Depends(get_store),
    identity: str | None = Depends(worker_identity),
) -> Response:
    try:
        store.reset_tu(tu_id, identity or req.agent, notes=req.notes)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    invalidate_dashboard_cache(request)
    log.info("reset_tu tu=%s actor=%s", tu_id, identity or req.agent)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
