from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from bp_work_server import __version__, history
from bp_work_server.decomp import DecompRepo
from bp_work_server.dependencies import auth_required
from bp_work_server.github import GitHubClient
from bp_work_server.routes import admin, dashboard, downloads, events, github, static, work
from bp_work_server.routes.static import static_dir
from bp_work_server.services.dashboard import warm_dashboard_state
from bp_work_server.store import WorkStore
from bp_work_server.sync import sync_workflow_repo

log = logging.getLogger(__name__)

__all__ = [
    "app",
    "auth_required",
    "create_app",
    "default_db_path",
    "default_users_db_path",
    "sync_workflow_repo",
]


def default_db_path() -> Path:
    return Path(os.environ.get("BP_WORK_DB", "data/bp-work.sqlite3"))


def default_users_db_path() -> Path:
    default = default_db_path().with_name(f"{default_db_path().stem}-users{default_db_path().suffix}")
    return Path(os.environ.get("BP_WORK_USERS_DB", default))


def create_app(store: WorkStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.dashboard_warm_task = asyncio.create_task(
            warm_dashboard_state(app, app.state.store)
        )
        # the Evolution panel gets a point every day whether or not anything was imported
        app.state.history_daily_task = asyncio.create_task(
            history.daily_task(app.state.store, logging.getLogger("bp_work_server.history"))
        )
        try:
            yield
        finally:
            for name in ("dashboard_warm_task", "attribution_warm_task", "history_daily_task"):
                warm_task = getattr(app.state, name, None)
                if warm_task and not warm_task.done():
                    warm_task.cancel()
                    try:
                        await warm_task
                    except asyncio.CancelledError:
                        pass
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
    app.state.attribution_warm_lock = asyncio.Lock()
    app.state.attribution_warm_task = None

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request failed method=%s path=%s", request.method, request.url.path)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        # Debug, not info: uvicorn already logs one access line per request, and
        # the dashboard is polled every few seconds by every open tab. Now that
        # app loggers actually reach the journal, this would double that volume
        # and bury the events worth reading.
        log.debug(
            "request method=%s path=%s status=%s elapsed_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    app.mount("/static", StaticFiles(directory=str(static_dir())), name="static")
    app.include_router(static.router)
    app.include_router(admin.router)
    app.include_router(work.router)
    app.include_router(dashboard.router)
    app.include_router(github.router)
    app.include_router(events.router)
    app.include_router(downloads.router)
    log.info("BP Work Server app initialized")
    return app


app = create_app()
