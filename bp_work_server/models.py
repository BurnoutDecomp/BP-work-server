from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


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


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
