from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from fastapi import BackgroundTasks, Request

from bp_work_server.attribution_cache import warm_attribution_cache
from bp_work_server.decomp import DecompRepo
from bp_work_server.services.attribution import repo_revision
from bp_work_server.store import WorkStore

log = logging.getLogger(__name__)

# How long a built dashboard snapshot is served before rebuilding. The dashboard
# is a polled overview, so a few seconds of staleness is invisible to users, but
# a 1s TTL meant near-every poll (clients poll ~5-7s apart) triggered a full
# ~5s rebuild, pinning a core continuously. 15s collapses that to a trickle.
DASHBOARD_CACHE_TTL = float(os.environ.get("BP_DASHBOARD_CACHE_TTL", "15"))


def cached_dashboard_state(
    request: Request,
    store: WorkStore,
    attribution_repo_rev: str | None = None,
) -> dict:
    now = time.monotonic()
    cache = request.app.state.dashboard_cache
    data = cache.get("data")
    if (
        data is not None
        and cache.get("attribution_repo_rev") == attribution_repo_rev
        and now < cache["expires_at"]
    ):
        return data
    with request.app.state.dashboard_cache_lock:
        now = time.monotonic()
        data = cache.get("data")
        if (
            data is not None
            and cache.get("attribution_repo_rev") == attribution_repo_rev
            and now < cache["expires_at"]
        ):
            return data
        started = time.perf_counter()
        data = store.dashboard_state(attribution_repo_rev=attribution_repo_rev)
        cache["data"] = data
        cache["attribution_repo_rev"] = attribution_repo_rev
        cache["expires_at"] = now + DASHBOARD_CACHE_TTL
        log.debug("dashboard_state built in %.3fs", time.perf_counter() - started)
        return data


def invalidate_dashboard_cache(request: Request) -> None:
    with request.app.state.dashboard_cache_lock:
        request.app.state.dashboard_cache["expires_at"] = 0.0
        request.app.state.dashboard_cache["data"] = None


def attribution_cache_needs_warm(data: dict[str, Any]) -> bool:
    coverage = data.get("attribution_cache") or {}
    return bool(
        coverage.get("repo_rev")
        and not (coverage.get("file_complete") and coverage.get("function_complete"))
    )


def is_attribution_warming(request: Request) -> bool:
    task = getattr(request.app.state, "attribution_warm_task", None)
    return bool(task and not task.done())


async def dashboard_state_response(
    request: Request,
    store: WorkStore,
    background_tasks: BackgroundTasks,
) -> dict:
    decomp: DecompRepo = request.app.state.decomp
    repo_rev = await repo_revision(decomp)
    data = cached_dashboard_state(request, store, attribution_repo_rev=repo_rev)
    data["attribution_cache_warming"] = is_attribution_warming(request)
    if attribution_cache_needs_warm(data):
        schedule_attribution_warm(request, store, decomp, background_tasks)
        data["attribution_cache_warming"] = True
    return data


def schedule_attribution_warm(
    request: Request,
    store: WorkStore,
    decomp: DecompRepo,
    background_tasks: BackgroundTasks,
) -> None:
    if is_attribution_warming(request):
        return
    background_tasks.add_task(_warm_attribution_cache_background, request, store, decomp)


async def _warm_attribution_cache_background(
    request: Request,
    store: WorkStore,
    decomp: DecompRepo,
) -> None:
    lock = request.app.state.attribution_warm_lock
    async with lock:
        if is_attribution_warming(request):
            return
        current = asyncio.current_task()
        request.app.state.attribution_warm_task = current
        started = time.perf_counter()
        try:
            await asyncio.to_thread(warm_attribution_cache, store, decomp)
            invalidate_dashboard_cache(request)
            log.info("attribution cache warmed in %.3fs", time.perf_counter() - started)
        except Exception:
            log.exception("attribution cache warm failed")
        finally:
            if getattr(request.app.state, "attribution_warm_task", None) is current:
                request.app.state.attribution_warm_task = None
