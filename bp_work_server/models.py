from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


TuStatus = Literal["todo", "in_progress", "compiled", "done", "blocked"]
ReviewVerdict = Literal["pass", "fail"]


class AgentRequest(BaseModel):
    agent: str = Field(min_length=1, max_length=160)


class ClaimRequest(AgentRequest):
    tu: str = Field(min_length=1)
    lease_seconds: int = Field(default=7200, ge=60, le=86_400)
    force: bool = False


class ClaimResponse(BaseModel):
    claimed: bool
    tu: str
    status: TuStatus
    owner: str | None = None
    lease_expires_at: datetime | None = None
    message: str | None = None


class HeartbeatRequest(AgentRequest):
    lease_seconds: int = Field(default=7200, ge=60, le=86_400)


class NextTu(BaseModel):
    id: str
    source: str | None = None
    n_funcs: int = 0
    n_decfigs: int = 0
    dest_path: str | None = None
    unresolved_deps: int | None = None


class NextResponse(BaseModel):
    active_goal: str | None
    count: int
    items: list[NextTu]


class ClaimNextRequest(AgentRequest):
    n: int = Field(default=1, ge=1, le=100)
    lease_seconds: int = Field(default=7200, ge=60, le=86_400)
    goal: str | None = None


class ClaimNextResponse(BaseModel):
    active_goal: str | None
    count: int
    claimed: list[ClaimResponse]


class StatusUpdateRequest(AgentRequest):
    notes: str | None = None
    commit: str | None = None
    files: list[str] = Field(default_factory=list)


class ReviewRequest(StatusUpdateRequest):
    verdict: ReviewVerdict


class BlockRequest(AgentRequest):
    reason: str = Field(min_length=1)


class TuRecord(BaseModel):
    id: str
    source: str | None
    status: TuStatus
    n_funcs: int
    n_decfigs: int
    dest_path: str | None
    owner: str | None
    notes: str | None
    updated_at: datetime | None
    lease_expires_at: datetime | None


class StatusCounts(BaseModel):
    todo: int = 0
    in_progress: int = 0
    compiled: int = 0
    done: int = 0
    blocked: int = 0


class SnapshotResponse(BaseModel):
    active_goal: str | None
    counts: StatusCounts
    tus: list[TuRecord]


class EventRecord(BaseModel):
    id: int
    ts: datetime
    tu_id: str | None
    agent: str | None
    action: str
    detail: dict[str, Any]


class EventsResponse(BaseModel):
    events: list[EventRecord]


class ImportResponse(BaseModel):
    tus: int
    funcs: int
    deps: int
    goals: int
    status_rows: int


class SyncRequest(BaseModel):
    branch: str | None = None
    commit: str | None = None
    reset: bool = False


class SyncResponse(ImportResponse):
    repo_url: str
    workflow_root: str
    branch: str
    commit: str


class ReconcileEventsRequest(BaseModel):
    actors: list[str] = Field(default_factory=list)
    apply: bool = False


class ReconcileEventsResponse(BaseModel):
    scanned_tus: int
    scanned_commits: int
    inserted: int
    skipped_existing_real: int
    skipped_existing_reconstructed: int
    skipped_actor_filter: int
    skipped_unresolved_actor: int
    applied: bool


class WorkerCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=160)
    is_admin: bool = False
    github_username: str | None = Field(default=None, max_length=160)


class WorkerResponse(BaseModel):
    token: str
    username: str
    is_admin: bool = False
    github_username: str | None = None


class WorkerInfo(BaseModel):
    token: str
    username: str
    active: bool
    is_admin: bool = False
    github_username: str | None = None
    created_at: datetime | None = None
    last_seen: datetime | None = None


class WorkerListResponse(BaseModel):
    workers: list[WorkerInfo]


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class DashboardStateResponse(FlexibleModel):
    server_time: datetime | None = None
    active_goal: str | None = None
    totals: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, Any] = Field(default_factory=dict)
    agents: list[dict[str, Any]] = Field(default_factory=list)
    active_work: list[dict[str, Any]] = Field(default_factory=list)
    next: dict[str, Any] = Field(default_factory=dict)
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    goals: list[dict[str, Any]] = Field(default_factory=list)
    blocked: list[dict[str, Any]] = Field(default_factory=list)
    actor_profiles: dict[str, str] = Field(default_factory=dict)
    attribution_cache: dict[str, Any] = Field(default_factory=dict)
    attribution_cache_warming: bool = False


class FacetsResponse(FlexibleModel):
    sources: list[str] = Field(default_factory=list)
    tu_statuses: list[str] = Field(default_factory=list)
    func_statuses: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    owners: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[dict[str, Any]]


class TuDetailResponse(FlexibleModel):
    id: str
    source: str | None = None
    status: str
    n_funcs: int = 0
    n_decfigs: int = 0
    dest_path: str | None = None
    owner: str | None = None
    notes: str | None = None
    updated_at: datetime | str | None = None
    lease_expires_at: datetime | str | None = None
    funcs: list[dict[str, Any]] = Field(default_factory=list)
    deps: list[dict[str, Any]] = Field(default_factory=list)
    dependents: list[dict[str, Any]] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)


class GoalDetailResponse(FlexibleModel):
    name: str
    total: int = 0
    done: int = 0
    remaining_count: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    ready: list[dict[str, Any]] = Field(default_factory=list)
    locked: list[dict[str, Any]] = Field(default_factory=list)
    blocked: list[dict[str, Any]] = Field(default_factory=list)


class ProfileResponse(FlexibleModel):
    name: str
    github_username: str | None = None
    registered: bool = False
    worker_active: bool = True
    is_admin: bool = False
    aliases: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    status_counts: dict[str, int] = Field(default_factory=dict)
    activity_by_day: list[dict[str, Any]] = Field(default_factory=list)
    action_counts: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    goals: list[dict[str, Any]] = Field(default_factory=list)
    active_work: list[dict[str, Any]] = Field(default_factory=list)
    top_tus: list[dict[str, Any]] = Field(default_factory=list)
    top_funcs: list[dict[str, Any]] = Field(default_factory=list)
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    attribution_cache: dict[str, Any] = Field(default_factory=dict)


class GitHubOverviewResponse(FlexibleModel):
    repo: dict[str, Any] = Field(default_factory=dict)
    info: dict[str, Any] | None = None
    commits: list[dict[str, Any]] = Field(default_factory=list)
    latest_commit: dict[str, Any] | None = None
    tree: dict[str, Any] | None = None
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    fetched_at: float | None = None


class FileHistoryResponse(BaseModel):
    history: dict[str, list[dict[str, Any]]]
