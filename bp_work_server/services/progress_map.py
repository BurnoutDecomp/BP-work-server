from __future__ import annotations

import time

from fastapi import Request

from bp_work_server.services.dashboard import DASHBOARD_CACHE_TTL
from bp_work_server.store import WorkStore


def cached_progress_map(request: Request, store: WorkStore, include_functions: bool) -> dict:
    # Two bounded entries; opening Functions loads the individual names lazily.
    with request.app.state.progress_map_cache_lock:
        cache = request.app.state.progress_map_cache
        entry = cache.get(include_functions)
        now = time.monotonic()
        if entry and now < entry["expires_at"]:
            return entry["data"]
        data = store.progress_map(include_functions=include_functions)
        cache[include_functions] = {"data": data, "expires_at": now + DASHBOARD_CACHE_TTL}
        return data
