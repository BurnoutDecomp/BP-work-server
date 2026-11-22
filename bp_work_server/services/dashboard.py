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

# The page itself is sent with the figures written in (services/page.py). When the cached
# state has expired it waits this long for a rebuild (well under a second on production),
# then goes out with the last state built rather than keep a reader waiting.
PAGE_STATE_WAIT = float(os.environ.get("BP_PAGE_STATE_WAIT", "2"))


async def decomp_health(decomp: DecompRepo) -> dict:
    """Clone freshness, or an empty dict for a repo object that cannot report it."""
    if not hasattr(decomp, "health"):
        return {}
    return await asyncio.to_thread(decomp.health)


def cached_dashboard_state(
    request: Request,
    store: WorkStore,
    attribution_repo_rev: str | None = None,
) -> dict:
    return build_dashboard_state(request.app, store, attribution_repo_rev=attribution_repo_rev)


def build_dashboard_state(
    app: Any,
    store: WorkStore,
    attribution_repo_rev: str | None = None,
) -> dict:
    now = time.monotonic()
    cache = app.state.dashboard_cache
    data = cache.get("data")
    if (
        data is not None
        and cache.get("attribution_repo_rev") == attribution_repo_rev
        and now < cache["expires_at"]
    ):
        return data
    with app.state.dashboard_cache_lock:
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
        # what the page shows while a rebuild runs; invalidation leaves it alone
        cache["last"] = data
        cache["attribution_repo_rev"] = attribution_repo_rev
        cache["expires_at"] = now + DASHBOARD_CACHE_TTL
        log.debug("dashboard_state built in %.3fs", time.perf_counter() - started)
        return data


async def warm_dashboard_state(app: Any, store: WorkStore) -> None:
    """Build the first dashboard state right after startup.

    The first build of a fresh process pays the per-agent contribution parse
    (seconds on a quiet box, ~15 s on a loaded one); every later build within
    the same process is memoised. Without this, the first visitor after every
    deploy watched the page time out once before it recovered.
    """
    try:
        repo_rev = await repo_revision(app.state.decomp)
        started = time.perf_counter()
        await asyncio.to_thread(build_dashboard_state, app, store, repo_rev)
        log.info("dashboard state warmed at startup in %.1fs", time.perf_counter() - started)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("dashboard state warm-up failed; the first request will build it")


async def dashboard_state_for_page(app: Any, wait: float = PAGE_STATE_WAIT) -> dict | None:
    """The state the page is sent with: the cached one while fresh, else a rebuild that
    comes within `wait` seconds, else the last one built (None before the first)."""
    cache = app.state.dashboard_cache
    data = cache.get("data")
    if data is not None and time.monotonic() < cache["expires_at"]:
        return data

    async def rebuild() -> dict:
        repo_rev = await repo_revision(app.state.decomp)
        return await asyncio.to_thread(build_dashboard_state, app, app.state.store, repo_rev)

    def report(done: asyncio.Future) -> None:
        if not done.cancelled() and done.exception() is not None:
            log.error("dashboard state for the page failed", exc_info=done.exception())

    # One rebuild at a time, however many pages are asked for while it runs. One that
    # outlasts the wait still lands in the cache, for the next reader, and its failure is
    # logged whenever it comes.
    task = getattr(app.state, "page_state_rebuild", None)
    if task is None or task.done():
        task = app.state.page_state_rebuild = asyncio.ensure_future(rebuild())
        task.add_done_callback(report)
    try:
        return await asyncio.wait_for(asyncio.shield(task), wait)
    except TimeoutError:
        log.info("dashboard state took over %.1fs; the page shows the last one built", wait)
    except Exception:
        pass  # report() logs it
    return cache.get("data") or cache.get("last")


async def warm_github(github: Any) -> None:
    """Read the repository from GitHub once right after startup.

    The page the server sends draws its repository panel from the last overview, and it
    never makes the first GitHub request itself (it would wait on it): without this, the
    page would go out with an empty panel until a browser had asked /github/overview.
    """
    try:
        await github.overview()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("GitHub warm-up failed; the first /github/overview will read it")


def invalidate_dashboard_cache(request: Request) -> None:
    with request.app.state.dashboard_cache_lock:
        request.app.state.dashboard_cache["expires_at"] = 0.0
        request.app.state.dashboard_cache["data"] = None
    with request.app.state.progress_map_cache_lock:
        request.app.state.progress_map_cache.clear()


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
    # Freshness of the clone every contribution number is blamed from. A clone
    # that quietly stops advancing (a stale git lock did exactly that for 41
    # days) is otherwise indistinguishable from contributors who stopped working.
    data["decomp_repo"] = await decomp_health(decomp)
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
