from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from bp_work_server import __version__
from bp_work_server.github import GitHubClient
from bp_work_server.models import (
    AgentRequest,
    BlockRequest,
    ClaimRequest,
    ClaimResponse,
    EventsResponse,
    HeartbeatRequest,
    ImportResponse,
    NextResponse,
    ReviewRequest,
    SnapshotResponse,
    StatusUpdateRequest,
    SyncRequest,
    SyncResponse,
)
from bp_work_server.store import WorkStore
from bp_work_server.sync import sync_workflow_repo


def default_db_path() -> Path:
    return Path(os.environ.get("BP_WORK_DB", "data/bp-work.sqlite3"))


def require_admin_token(x_bp_admin_token: str | None = Header(default=None)) -> None:
    expected = os.environ.get("BP_WORK_ADMIN_TOKEN")
    if not expected:
        return
    if not x_bp_admin_token or not secrets.compare_digest(x_bp_admin_token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin token")


def create_app(store: WorkStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.github.aclose()

    app = FastAPI(
        title="BP Work Server",
        version=__version__,
        description="Coordination API for Burnout Paradise decompilation work claims.",
        lifespan=lifespan,
    )
    app.state.store = store or WorkStore(default_db_path())
    app.state.store.migrate()
    app.state.github = GitHubClient()
    static_dir = files("bp_work_server").joinpath("static")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def get_store() -> WorkStore:
        return app.state.store

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return static_dir.joinpath("index.html").read_text(encoding="utf-8")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"ok": "true", "version": __version__}

    @app.post("/admin/import", response_model=ImportResponse)
    def import_workflow(
        workflow_root: str = Query(..., description="Path to BP-Decomp_Workflow"),
        reset: bool = Query(False),
        _admin: None = Depends(require_admin_token),
        store: WorkStore = Depends(get_store),
    ) -> dict[str, int]:
        try:
            return store.import_workflow(workflow_root, reset=reset)
        except FileNotFoundError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    @app.post("/admin/sync", response_model=SyncResponse)
    def sync_workflow(
        req: SyncRequest,
        _admin: None = Depends(require_admin_token),
        store: WorkStore = Depends(get_store),
    ) -> dict:
        try:
            return sync_workflow_repo(store, branch=req.branch, reset=req.reset)
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    @app.get("/snapshot", response_model=SnapshotResponse)
    def snapshot(
        include_tus: bool = Query(True),
        store: WorkStore = Depends(get_store),
    ) -> SnapshotResponse:
        active_goal, counts, tus = store.snapshot(include_tus=include_tus)
        return SnapshotResponse(active_goal=active_goal, counts=counts, tus=tus)

    @app.get("/next", response_model=NextResponse)
    def next_tus(
        n: int = Query(1, ge=1, le=100),
        goal: str | None = Query(None),
        store: WorkStore = Depends(get_store),
    ) -> NextResponse:
        active_goal, items = store.next_tus(n=n, goal=goal)
        return NextResponse(active_goal=active_goal, count=len(items), items=items)

    @app.post("/claims", response_model=ClaimResponse, status_code=status.HTTP_201_CREATED)
    def claim(req: ClaimRequest, response: Response, store: WorkStore = Depends(get_store)):
        try:
            result = store.claim(req.tu, req.agent, req.lease_seconds, force=req.force)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        if not result.claimed:
            response.status_code = status.HTTP_409_CONFLICT
        return result

    @app.post("/claims/{tu_id:path}/heartbeat", response_model=ClaimResponse)
    def heartbeat(
        tu_id: str,
        req: HeartbeatRequest,
        response: Response,
        store: WorkStore = Depends(get_store),
    ):
        try:
            result = store.heartbeat(tu_id, req.agent, req.lease_seconds)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        if not result.claimed:
            response.status_code = status.HTTP_409_CONFLICT
        return result

    @app.delete("/claims/{tu_id:path}", status_code=status.HTTP_204_NO_CONTENT)
    def release(tu_id: str, req: AgentRequest, store: WorkStore = Depends(get_store)) -> Response:
        try:
            store.release(tu_id, req.agent)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/tu/{tu_id:path}/compiled", status_code=status.HTTP_204_NO_CONTENT)
    def compiled(
        tu_id: str,
        req: StatusUpdateRequest,
        store: WorkStore = Depends(get_store),
    ) -> Response:
        try:
            store.mark_compiled(tu_id, req.agent, notes=req.notes, commit=req.commit)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/tu/{tu_id:path}/review", status_code=status.HTTP_204_NO_CONTENT)
    def review(tu_id: str, req: ReviewRequest, store: WorkStore = Depends(get_store)) -> Response:
        try:
            store.review(tu_id, req.agent, req.verdict, notes=req.notes, commit=req.commit)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/tu/{tu_id:path}/block", status_code=status.HTTP_204_NO_CONTENT)
    def block(tu_id: str, req: BlockRequest, store: WorkStore = Depends(get_store)) -> Response:
        try:
            store.block(tu_id, req.agent, req.reason)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/tu/{tu_id:path}/unblock", status_code=status.HTTP_204_NO_CONTENT)
    def unblock(tu_id: str, req: AgentRequest, store: WorkStore = Depends(get_store)) -> Response:
        try:
            store.unblock(tu_id, req.agent)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/events", response_model=EventsResponse)
    def events(
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        store: WorkStore = Depends(get_store),
    ) -> EventsResponse:
        return EventsResponse(events=store.events(after=after, limit=limit))

    @app.get("/dashboard/state")
    def dashboard_state(store: WorkStore = Depends(get_store)) -> dict:
        return store.dashboard_state()

    @app.get("/github/overview")
    async def github_overview() -> dict:
        """Cached GitHub repo info, recent commits, and file tree for the dev branch."""
        return await app.state.github.overview()

    @app.get("/events/stream")
    async def event_stream(
        after: int = Query(0, ge=0),
        store: WorkStore = Depends(get_store),
    ) -> StreamingResponse:
        async def stream():
            last_id = after
            yield "event: connected\ndata: {}\n\n"
            while True:
                events = store.events(after=last_id, limit=100)
                for event in events:
                    last_id = max(last_id, event["id"])
                    payload = json.dumps(event, default=str)
                    yield f"id: {event['id']}\nevent: work-event\ndata: {payload}\n\n"
                yield "event: tick\ndata: {}\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app()
