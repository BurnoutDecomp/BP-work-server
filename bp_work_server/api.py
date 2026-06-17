from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from bp_work_server import __version__
from bp_work_server.decomp import DecompRepo
from bp_work_server.github import GitHubClient, login_from_noreply_email
from bp_work_server.models import (
    AgentRequest,
    BlockRequest,
    ClaimNextRequest,
    ClaimNextResponse,
    ClaimRequest,
    ClaimResponse,
    EventsResponse,
    HeartbeatRequest,
    ImportResponse,
    NextResponse,
    ReviewRequest,
    ReconcileEventsRequest,
    ReconcileEventsResponse,
    SnapshotResponse,
    StatusUpdateRequest,
    SyncRequest,
    SyncResponse,
    WorkerCreateRequest,
    WorkerListResponse,
    WorkerResponse,
)
from bp_work_server.store import WorkStore
from bp_work_server.reconcile_events import reconcile_review_events_from_decomp
from bp_work_server.sync import sync_workflow_repo


def default_db_path() -> Path:
    return Path(os.environ.get("BP_WORK_DB", "data/bp-work.sqlite3"))


def default_users_db_path() -> Path:
    default = default_db_path().with_name(f"{default_db_path().stem}-users{default_db_path().suffix}")
    return Path(os.environ.get("BP_WORK_USERS_DB", default))


def auth_required() -> bool:
    """Every work mutation needs a valid X-Work-Token (a server-issued worker id). This
    is what lets the server URL be public: the token is the gate, not the URL. ON by
    default; set BP_WORK_REQUIRE_TOKEN to a falsey value (0/false/no/off) to disable it
    for a fully private/trusted deployment."""
    return os.environ.get("BP_WORK_REQUIRE_TOKEN", "1").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


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
    app.state.store = store or WorkStore(default_db_path(), default_users_db_path())
    app.state.store.migrate()
    app.state.github = GitHubClient()
    app.state.decomp = DecompRepo()
    app.state.dashboard_cache = {"expires_at": 0.0, "data": None}
    app.state.dashboard_cache_lock = threading.Lock()
    static_dir = files("bp_work_server").joinpath("static")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def get_store() -> WorkStore:
        return app.state.store

    def invalidate_dashboard_cache() -> None:
        with app.state.dashboard_cache_lock:
            app.state.dashboard_cache["expires_at"] = 0.0
            app.state.dashboard_cache["data"] = None

    def cached_dashboard_state(store: WorkStore) -> dict:
        now = time.monotonic()
        cache = app.state.dashboard_cache
        data = cache.get("data")
        if data is not None and now < cache["expires_at"]:
            return data
        with app.state.dashboard_cache_lock:
            now = time.monotonic()
            data = cache.get("data")
            if data is not None and now < cache["expires_at"]:
                return data
            data = store.dashboard_state()
            cache["data"] = data
            cache["expires_at"] = now + 1.0
            return data

    def worker_identity(
        x_work_token: str | None = Header(default=None),
        store: WorkStore = Depends(get_store),
    ) -> str | None:
        """Resolve the caller to a username from their X-Work-Token. Returns None when
        auth is disabled (callers then fall back to the body `agent`). When auth is on,
        a missing/invalid/revoked token is rejected -- the username, never the token, is
        what gets recorded as owner."""
        if not auth_required():
            return None
        username = store.resolve_worker(x_work_token or "")
        if not username:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "missing or invalid worker token (X-Work-Token)",
            )
        return username

    def require_admin_worker(
        x_work_token: str | None = Header(default=None),
        store: WorkStore = Depends(get_store),
    ) -> str:
        """Gate /admin/* on a worker whose id carries the admin role. Replaces the old
        shared admin secret: admin is now per-user, granted server-side. Bootstrap the
        first admin with the `bp-work-server worker add --admin` CLI (direct DB)."""
        token = x_work_token or ""
        if not store.resolve_worker(token):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "missing or invalid worker token (X-Work-Token)",
            )
        username = store.resolve_admin(token)
        if not username:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "admin privileges required")
        return username

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return static_dir.joinpath("index.html").read_text(encoding="utf-8")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"ok": "true", "version": __version__}

    @app.post("/admin/workers", response_model=WorkerResponse, status_code=status.HTTP_201_CREATED)
    def create_worker(
        req: WorkerCreateRequest,
        _admin: str = Depends(require_admin_worker),
        store: WorkStore = Depends(get_store),
    ) -> WorkerResponse:
        result = store.create_worker(
            req.username, is_admin=req.is_admin, github_username=req.github_username
        )
        invalidate_dashboard_cache()
        return WorkerResponse(**result)

    @app.get("/admin/workers", response_model=WorkerListResponse)
    def list_workers(
        _admin: str = Depends(require_admin_worker),
        store: WorkStore = Depends(get_store),
    ) -> WorkerListResponse:
        return WorkerListResponse(workers=store.list_workers())

    @app.delete("/admin/workers/{token}", status_code=status.HTTP_204_NO_CONTENT)
    def revoke_worker(
        token: str,
        _admin: str = Depends(require_admin_worker),
        store: WorkStore = Depends(get_store),
    ) -> Response:
        if not store.revoke_worker(token):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown worker token")
        invalidate_dashboard_cache()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/admin/import", response_model=ImportResponse)
    def import_workflow(
        workflow_root: str = Query(..., description="Path to BP-Decomp_Workflow"),
        reset: bool = Query(False),
        _admin: str = Depends(require_admin_worker),
        store: WorkStore = Depends(get_store),
    ) -> dict[str, int]:
        try:
            result = store.import_workflow(workflow_root, reset=reset)
            invalidate_dashboard_cache()
            return result
        except FileNotFoundError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    @app.post("/admin/sync", response_model=SyncResponse)
    def sync_workflow(
        req: SyncRequest,
        _admin: str = Depends(require_admin_worker),
        store: WorkStore = Depends(get_store),
    ) -> dict:
        try:
            result = sync_workflow_repo(store, branch=req.branch, reset=req.reset)
            invalidate_dashboard_cache()
            return result
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    @app.post("/admin/reconcile-events", response_model=ReconcileEventsResponse)
    async def reconcile_events(
        req: ReconcileEventsRequest,
        _admin: str = Depends(require_admin_worker),
        store: WorkStore = Depends(get_store),
    ) -> ReconcileEventsResponse:
        result = await asyncio.to_thread(
            reconcile_review_events_from_decomp,
            store,
            app.state.decomp,
            actors=set(req.actors or []),
            apply=req.apply,
        )
        invalidate_dashboard_cache()
        return ReconcileEventsResponse(
            scanned_tus=result.scanned_tus,
            scanned_commits=result.scanned_commits,
            inserted=result.inserted,
            skipped_existing_real=result.skipped_existing_real,
            skipped_existing_reconstructed=result.skipped_existing_reconstructed,
            skipped_actor_filter=result.skipped_actor_filter,
            skipped_unresolved_actor=result.skipped_unresolved_actor,
            applied=req.apply,
        )

    @app.get("/export/status")
    def export_status(store: WorkStore = Depends(get_store)) -> dict:
        """The committed ``progress/status.json`` regenerated from the live DB.

        Open read (the durable done/blocked it returns is already in ``/snapshot``):
        a CI job fetches this and commits it to the workflow repo, so decomp workers
        never have to push status.json by hand. See docs/protocol.md."""
        return store.export_status()

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
    def claim(
        req: ClaimRequest,
        response: Response,
        store: WorkStore = Depends(get_store),
        identity: str | None = Depends(worker_identity),
    ):
        try:
            result = store.claim(req.tu, identity or req.agent, req.lease_seconds, force=req.force)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        invalidate_dashboard_cache()
        if not result.claimed:
            response.status_code = status.HTTP_409_CONFLICT
        return result

    @app.post("/claims/next", response_model=ClaimNextResponse)
    def claim_next(
        req: ClaimNextRequest,
        store: WorkStore = Depends(get_store),
        identity: str | None = Depends(worker_identity),
    ) -> ClaimNextResponse:
        active_goal, claimed = store.claim_next(
            identity or req.agent, n=req.n, lease_seconds=req.lease_seconds, goal=req.goal
        )
        invalidate_dashboard_cache()
        return ClaimNextResponse(active_goal=active_goal, count=len(claimed), claimed=claimed)

    @app.post("/claims/{tu_id:path}/heartbeat", response_model=ClaimResponse)
    def heartbeat(
        tu_id: str,
        req: HeartbeatRequest,
        response: Response,
        store: WorkStore = Depends(get_store),
        identity: str | None = Depends(worker_identity),
    ):
        try:
            result = store.heartbeat(tu_id, identity or req.agent, req.lease_seconds)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        invalidate_dashboard_cache()
        if not result.claimed:
            response.status_code = status.HTTP_409_CONFLICT
        return result

    @app.delete("/claims/{tu_id:path}", status_code=status.HTTP_204_NO_CONTENT)
    def release(
        tu_id: str,
        req: AgentRequest,
        store: WorkStore = Depends(get_store),
        identity: str | None = Depends(worker_identity),
    ) -> Response:
        try:
            store.release(tu_id, identity or req.agent)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        invalidate_dashboard_cache()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/tu/{tu_id:path}/compiled", status_code=status.HTTP_204_NO_CONTENT)
    def compiled(
        tu_id: str,
        req: StatusUpdateRequest,
        store: WorkStore = Depends(get_store),
        identity: str | None = Depends(worker_identity),
    ) -> Response:
        try:
            store.mark_compiled(tu_id, identity or req.agent, notes=req.notes, commit=req.commit)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        invalidate_dashboard_cache()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/tu/{tu_id:path}/review", status_code=status.HTTP_204_NO_CONTENT)
    def review(
        tu_id: str,
        req: ReviewRequest,
        store: WorkStore = Depends(get_store),
        identity: str | None = Depends(worker_identity),
    ) -> Response:
        try:
            store.review(tu_id, identity or req.agent, req.verdict, notes=req.notes, commit=req.commit)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        invalidate_dashboard_cache()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/tu/{tu_id:path}/block", status_code=status.HTTP_204_NO_CONTENT)
    def block(
        tu_id: str,
        req: BlockRequest,
        store: WorkStore = Depends(get_store),
        identity: str | None = Depends(worker_identity),
    ) -> Response:
        try:
            store.block(tu_id, identity or req.agent, req.reason)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        invalidate_dashboard_cache()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/tu/{tu_id:path}/unblock", status_code=status.HTTP_204_NO_CONTENT)
    def unblock(
        tu_id: str,
        req: AgentRequest,
        store: WorkStore = Depends(get_store),
        identity: str | None = Depends(worker_identity),
    ) -> Response:
        try:
            store.unblock(tu_id, identity or req.agent)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        invalidate_dashboard_cache()
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
        return cached_dashboard_state(store)

    @app.get("/api/facets")
    def facets(store: WorkStore = Depends(get_store)) -> dict:
        return store.facets()

    async def file_attrs(dest_paths: set[str]) -> dict[str, dict]:
        if not dest_paths:
            return {}
        decomp: DecompRepo = app.state.decomp

        def resolve_histories() -> dict[str, dict]:
            histories: dict[str, dict] = {}
            for dest_path in dest_paths:
                history = decomp.history(dest_path)
                if history:
                    histories[dest_path] = history[0]
            return histories

        histories = await asyncio.to_thread(resolve_histories)
        if not histories:
            return {}

        login_map = await app.state.github.author_login_map()
        aliases, _profiles = await asyncio.to_thread(app.state.store.actor_maps)
        return {
            dest_path: attribute_commit(commit, login_map, aliases)
            for dest_path, commit in histories.items()
        }

    def apply_tu_file_attr(item: dict, attr: dict | None) -> None:
        if not attr or not attr.get("author"):
            return
        item["updated_at"] = attr["date"]
        item["completed_by"] = attr["author"]
        item["completed_by_login"] = attr["login"]

    def hide_import_timestamp_for_idle_todo(item: dict) -> None:
        if (
            item.get("status") == "todo"
            and not item.get("owner")
            and not item.get("completed_by")
            and not item.get("last_actor")
        ):
            item["updated_at"] = None

    @app.get("/api/tus")
    async def search_tus(
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
            q=q, statuses=status, source=source, goal=goal, owner=owner,
            sort=sort, order=order, limit=limit, offset=offset,
        )
        done_dest_paths = {
            item.get("dest_path")
            for item in result.get("items", [])
            if item.get("status") == "done" and item.get("dest_path")
        }
        attrs_by_dest = await file_attrs(done_dest_paths)
        for item in result.get("items", []):
            apply_tu_file_attr(item, attrs_by_dest.get(item.get("dest_path")))
            hide_import_timestamp_for_idle_todo(item)
        return result

    @app.get("/api/tu")
    async def tu_detail(
        id: str = Query(..., min_length=1),
        store: WorkStore = Depends(get_store),
    ) -> dict:
        try:
            detail = await asyncio.to_thread(store.tu_detail, id)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        decomp: DecompRepo = app.state.decomp
        dest_path = detail.get("dest_path")
        # Headers are inlined into the .cpp, so a .h destination has no file of
        # its own; expose the path that actually exists so the UI links there.
        repo_path = decomp.repo_path(dest_path)
        if repo_path:
            detail["repo_path"] = repo_path
        # The committed file is the real record: show its last commit's date and
        # author rather than the backfilled import time / guessed completer.
        attrs_by_dest = await file_attrs({dest_path} if dest_path else set())
        attr = attrs_by_dest.get(dest_path)
        apply_tu_file_attr(detail, attr)
        hide_import_timestamp_for_idle_todo(detail)
        if attr and attr.get("author"):
            for func in detail.get("funcs", []):
                if func.get("status") != "todo":
                    func["completed_by"] = attr["author"]
                    func["completed_by_login"] = attr["login"]
                    func["completed_at"] = attr["date"]
        return detail

    @app.get("/api/funcs")
    async def search_funcs(
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
        return result

    @app.get("/github/overview")
    async def github_overview() -> dict:
        """Cached GitHub repo info, recent commits, and file tree for the dev branch."""
        return await app.state.github.overview()

    def attribute_commit(commit: dict, login_map: dict[str, str], aliases: dict[str, str]) -> dict:
        """Turn a raw git commit into display fields, resolving the GitHub login.

        Login comes from the API email->login map, falling back to the login
        embedded in a noreply email, then to None. Display names are canonical
        worker usernames when an override/case-insensitive alias exists.
        """
        email = commit.get("email") or ""
        login = login_map.get(email.lower()) or login_from_noreply_email(email)
        candidates = [login, email, commit.get("name")]
        author = None
        for candidate in candidates:
            cleaned = str(candidate).strip() if candidate is not None else ""
            if not cleaned:
                continue
            author = aliases.get(cleaned.lower())
            if author:
                break
            if candidate == commit.get("name"):
                author = cleaned
                break
        return {"date": commit["date"], "author": author, "login": login}

    @app.get("/events/file-history")
    async def events_file_history(store: WorkStore = Depends(get_store)) -> dict:
        """Per-file commit history for backfilled Live Events, keyed by TU id.

        Those rows ("workflow commit delta" / "legacy pre-server attribution")
        share one import timestamp, one meaningless commit SHA, and a guessed
        author. The truth is each file's own commit history, so we return every
        commit (2026+) that touched the TU's destination file -- GitHub author
        and date -- and the dashboard expands each backfilled row into one event
        per commit. TUs whose file has no qualifying commits are omitted; the
        dashboard keeps the original row for them.
        """
        targets = await asyncio.to_thread(store.backfilled_event_targets)
        login_map = await app.state.github.author_login_map()
        aliases, _profiles = await asyncio.to_thread(store.actor_maps)
        decomp: DecompRepo = app.state.decomp

        def resolve() -> dict[str, list]:
            history: dict[str, list] = {}
            for tu_id, dest_path in targets.items():
                commits = decomp.history(dest_path)
                if commits:
                    history[tu_id] = [attribute_commit(c, login_map, aliases) for c in commits]
            return history

        return {"history": await asyncio.to_thread(resolve)}

    @app.get("/events/stream")
    async def event_stream(
        after: int = Query(0, ge=0),
        store: WorkStore = Depends(get_store),
    ) -> StreamingResponse:
        async def stream():
            last_id = after
            yield "event: connected\ndata: {}\n\n"
            while True:
                events = await asyncio.to_thread(store.events, after=last_id, limit=100)
                for event in events:
                    last_id = max(last_id, event["id"])
                    payload = json.dumps(event, default=str)
                    yield f"id: {event['id']}\nevent: work-event\ndata: {payload}\n\n"
                yield ": keepalive\n\n"
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
