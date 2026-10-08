from __future__ import annotations

import os

from fastapi import Depends, Header, HTTPException, Request, status

from bp_work_server.store import WorkStore


def auth_required() -> bool:
    """Whether work mutations require a valid X-Work-Token."""
    return os.environ.get("BP_WORK_REQUIRE_TOKEN", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )


def get_store(request: Request) -> WorkStore:
    return request.app.state.store


def invalidate_dashboard_cache(request: Request) -> None:
    from bp_work_server.services.dashboard import invalidate_dashboard_cache as invalidate

    invalidate(request)


def worker_identity(
    x_work_token: str | None = Header(default=None),
    store: WorkStore = Depends(get_store),
) -> str | None:
    """Resolve the caller to a username from their X-Work-Token."""
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
