from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status

from bp_work_server.decomp import DecompRepo
from bp_work_server.dependencies import get_store
from bp_work_server.models import (
    AuditFileResponse,
    AuditSummaryResponse,
    DashboardStateResponse,
    FacetsResponse,
    GoalDetailResponse,
    ProfileResponse,
    SearchResponse,
    TuDetailResponse,
)
from bp_work_server.services.attribution import (
    AttributionService,
    apply_tu_file_attr,
    hide_import_timestamp_for_idle_todo,
    repo_revision,
)
from bp_work_server.services.dashboard import dashboard_state_response
from bp_work_server.store import WorkStore

router = APIRouter()


def attribution_service(request: Request, store: WorkStore) -> AttributionService:
    return AttributionService(store, request.app.state.decomp)


@router.get("/dashboard/state", response_model=DashboardStateResponse)
async def dashboard_state(
    request: Request,
    background_tasks: BackgroundTasks,
    store: WorkStore = Depends(get_store),
) -> dict:
    return await dashboard_state_response(request, store, background_tasks)


@router.get("/api/facets", response_model=FacetsResponse)
def facets(store: WorkStore = Depends(get_store)) -> dict:
    return store.facets()


# ---- the evidence layer: CI's per-commit glue audit + stub inventory ----
@router.get("/api/audit/summary", response_model=AuditSummaryResponse)
async def audit_summary(store: WorkStore = Depends(get_store)) -> dict:
    return await asyncio.to_thread(store.audit_summary)


@router.get("/api/audit/files", response_model=SearchResponse)
async def audit_files(
    q: str | None = Query(None),
    category: str | None = Query(None),
    sort: str = Query(
        "weight",
        pattern="^(weight|functions|no_body|missing_case|missing_event|missing_callee|missing_assert|file)$",
    ),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    store: WorkStore = Depends(get_store),
) -> dict:
    return await asyncio.to_thread(
        store.audit_files,
        q=q,
        category=category,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )


@router.get("/api/audit/functions", response_model=AuditFileResponse)
async def audit_functions(
    file: str = Query(..., min_length=1),
    store: WorkStore = Depends(get_store),
) -> dict:
    return await asyncio.to_thread(store.audit_functions, file)


@router.get("/api/stubs/files", response_model=SearchResponse)
async def stub_files(
    q: str | None = Query(None),
    tier: str | None = Query(None, pattern="^(HIGH|MEDIUM|LOW|high|medium|low)$"),
    live: bool = Query(False),
    sort: str = Query("stubs", pattern="^(stubs|high|live|console_lines|file)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    store: WorkStore = Depends(get_store),
) -> dict:
    return await asyncio.to_thread(
        store.stub_files,
        q=q,
        tier=tier,
        live_only=live,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )


@router.get("/api/stubs", response_model=AuditFileResponse)
async def stubs(
    file: str = Query(..., min_length=1),
    store: WorkStore = Depends(get_store),
) -> dict:
    return await asyncio.to_thread(store.stubs, file)


@router.get("/api/goal", response_model=GoalDetailResponse)
def goal_detail(
    name: str = Query(..., min_length=1),
    store: WorkStore = Depends(get_store),
) -> dict:
    try:
        return store.goal_detail(name)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/api/profile", response_model=ProfileResponse)
async def profile_detail(
    request: Request,
    name: str = Query(..., min_length=1),
    store: WorkStore = Depends(get_store),
) -> dict:
    decomp: DecompRepo = request.app.state.decomp
    repo_rev = await repo_revision(decomp)
    try:
        return await asyncio.to_thread(store.actor_profile, name, repo_rev)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/api/tus", response_model=SearchResponse)
async def search_tus(
    request: Request,
    q: str | None = Query(None),
    status: list[str] | None = Query(None),
    source: str | None = Query(None),
    goal: str | None = Query(None),
    owner: str | None = Query(None),
    sort: str = Query("id", pattern="^(id|funcs|updated|status|queue)$"),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    store: WorkStore = Depends(get_store),
) -> dict:
    result = await asyncio.to_thread(
        store.search_tus,
        q=q,
        statuses=status,
        source=source,
        goal=goal,
        owner=owner,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    done_dest_paths = {
        item.get("dest_path")
        for item in result.get("items", [])
        if item.get("status") == "done" and item.get("dest_path")
    }
    service = attribution_service(request, store)
    attrs_by_dest = await service.file_attrs(done_dest_paths)
    for item in result.get("items", []):
        apply_tu_file_attr(item, attrs_by_dest.get(item.get("dest_path")))
        hide_import_timestamp_for_idle_todo(item)
    return result


@router.get("/api/tu", response_model=TuDetailResponse)
async def tu_detail(
    request: Request,
    id: str = Query(..., min_length=1),
    store: WorkStore = Depends(get_store),
) -> dict:
    try:
        detail = await asyncio.to_thread(store.tu_detail, id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    decomp: DecompRepo = request.app.state.decomp
    dest_path = detail.get("dest_path")
    repo_path = decomp.repo_path(dest_path)
    if repo_path:
        detail["repo_path"] = repo_path

    service = attribution_service(request, store)
    attr_dest_paths = {dest_path} if dest_path and detail.get("status") == "done" else set()
    attrs_by_dest = await service.file_attrs(attr_dest_paths)
    attr = attrs_by_dest.get(dest_path)
    apply_tu_file_attr(detail, attr)
    hide_import_timestamp_for_idle_todo(detail)
    if attr and attr.get("primary_contributor"):
        for func in detail.get("funcs", []):
            if func.get("status") != "todo":
                func["completed_by"] = attr["primary_contributor"]
                func["completed_by_login"] = attr.get("primary_contributor_login")
                func["completed_at"] = attr.get("latest_change_at")
                func["contributors"] = attr.get("contributors", [])
                func["contributor_count"] = attr.get("contributor_count", 0)
                func["primary_contributor"] = attr["primary_contributor"]
                func["primary_contributor_login"] = attr.get("primary_contributor_login")
                func["primary_contributor_lines"] = attr.get("primary_contributor_lines")
                func["primary_contributor_percent"] = attr.get("primary_contributor_percent")
                func["attribution_basis"] = attr.get("attribution_basis")
                func["function_range_found"] = False
                func["line_range"] = None
    detail_func_items = [{**func, "tu_dest_path": dest_path} for func in detail.get("funcs", [])]
    await service.function_attrs(detail_func_items)
    func_attrs = {func["name"]: func for func in detail_func_items}
    for func in detail.get("funcs", []):
        enriched = func_attrs.get(func["name"])
        if enriched:
            func.update(enriched)
    return detail


@router.get("/api/funcs", response_model=SearchResponse)
async def search_funcs(
    request: Request,
    q: str | None = Query(None),
    status: list[str] | None = Query(None),
    tu: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    store: WorkStore = Depends(get_store),
) -> dict:
    result = await asyncio.to_thread(
        store.search_funcs, q=q, statuses=status, tu=tu, limit=limit, offset=offset
    )
    await attribution_service(request, store).function_attrs(result.get("items", []))
    return result
