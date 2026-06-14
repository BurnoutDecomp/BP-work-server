"""GitHub API proxy with aggressive caching to stay under rate limits.

All browser clients talk to this server, never to GitHub directly, so a single
process-wide cache serves every dashboard viewer from one upstream request.

Two layers protect the GitHub rate limit:

1. Per-resource TTL: we do not even contact GitHub again until the TTL expires.
2. Conditional requests (ETag / If-None-Match): when the TTL does expire we
   revalidate with the stored ETag. GitHub returns ``304 Not Modified`` when
   nothing changed, and **304 responses do not count against the rate limit**.

So a steady dashboard costs at most one *counted* request per resource each time
the underlying data actually changes. An optional ``GITHUB_TOKEN`` raises the
unauthenticated 60 req/hour ceiling to 5000 req/hour.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"

# Repository the dashboard mirrors. Overridable via environment for forks.
REPO_OWNER = os.environ.get("BP_GITHUB_OWNER", "Adriwin06")
REPO_NAME = os.environ.get("BP_GITHUB_REPO", "b5-decomp")
REPO_REF = os.environ.get("BP_GITHUB_REF", "dev")

# How long a cached resource is served before we revalidate upstream (seconds).
TTL_REPO = 300
TTL_COMMITS = 180
TTL_TREE = 600

# Cap the tree we ship to the browser; the full recursive tree can be huge.
TREE_LIMIT = 4000


@dataclass
class CacheEntry:
    data: Any = None
    etag: str | None = None
    fetched_at: float = 0.0
    error: str | None = None


@dataclass
class GitHubClient:
    owner: str = REPO_OWNER
    repo: str = REPO_NAME
    ref: str = REPO_REF
    token: str | None = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN"))

    _cache: dict[str, CacheEntry] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _client: httpx.AsyncClient | None = None
    rate: dict[str, Any] = field(default_factory=dict)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bp-work-server-dashboard",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0, headers=self._headers())
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _record_rate(self, resp: httpx.Response) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is None:
            return
        reset = resp.headers.get("X-RateLimit-Reset")
        self.rate = {
            "remaining": int(remaining),
            "limit": int(resp.headers.get("X-RateLimit-Limit", 0)),
            "reset": int(reset) if reset else None,
            "authenticated": bool(self.token),
        }

    async def _fetch(self, key: str, url: str, ttl: int, transform) -> CacheEntry:
        """Return a cache entry for ``key``, revalidating against GitHub if stale."""
        entry = self._cache.get(key)
        now = time.time()
        if entry and entry.data is not None and (now - entry.fetched_at) < ttl:
            return entry

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Another coroutine may have refreshed while we waited for the lock.
            entry = self._cache.get(key)
            now = time.time()
            if entry and entry.data is not None and (now - entry.fetched_at) < ttl:
                return entry

            client = await self._get_client()
            headers: dict[str, str] = {}
            if entry and entry.etag:
                headers["If-None-Match"] = entry.etag

            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                fresh = entry or CacheEntry()
                fresh.error = f"github request failed: {exc}"
                self._cache[key] = fresh
                return fresh

            self._record_rate(resp)

            if resp.status_code == 304 and entry:
                # Free revalidation: data unchanged, did not count against limit.
                entry.fetched_at = now
                entry.error = None
                return entry

            if resp.status_code == 200:
                new_entry = CacheEntry(
                    data=transform(resp.json()),
                    etag=resp.headers.get("ETag"),
                    fetched_at=now,
                    error=None,
                )
                self._cache[key] = new_entry
                return new_entry

            # Rate limited or other error: keep serving stale data if we have it.
            fresh = entry or CacheEntry()
            if resp.status_code == 403 and self.rate.get("remaining") == 0:
                fresh.error = "github rate limit reached; serving cached data"
            else:
                fresh.error = f"github returned {resp.status_code}"
            fresh.fetched_at = now if fresh.data is None else fresh.fetched_at
            self._cache[key] = fresh
            return fresh

    async def fetch_repo(self) -> CacheEntry:
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}"

        def transform(d: dict) -> dict:
            return {
                "full_name": d.get("full_name"),
                "description": d.get("description"),
                "html_url": d.get("html_url"),
                "default_branch": d.get("default_branch"),
                "stargazers_count": d.get("stargazers_count"),
                "forks_count": d.get("forks_count"),
                "open_issues_count": d.get("open_issues_count"),
                "watchers_count": d.get("subscribers_count") or d.get("watchers_count"),
                "language": d.get("language"),
                "pushed_at": d.get("pushed_at"),
                "license": (d.get("license") or {}).get("spdx_id"),
            }

        return await self._fetch("repo", url, TTL_REPO, transform)

    async def fetch_commits(self, count: int = 8) -> CacheEntry:
        url = (
            f"{GITHUB_API}/repos/{self.owner}/{self.repo}/commits"
            f"?sha={self.ref}&per_page={count}"
        )

        def transform(items: list[dict]) -> list[dict]:
            out = []
            for c in items:
                commit = c.get("commit") or {}
                author = commit.get("author") or {}
                gh_author = c.get("author") or {}
                out.append(
                    {
                        "sha": c.get("sha"),
                        "short_sha": (c.get("sha") or "")[:7],
                        "message": (commit.get("message") or "").split("\n", 1)[0],
                        "author": author.get("name"),
                        "login": gh_author.get("login"),
                        "avatar_url": gh_author.get("avatar_url"),
                        "date": author.get("date"),
                        "html_url": c.get("html_url"),
                    }
                )
            return out

        return await self._fetch(f"commits:{count}", url, TTL_COMMITS, transform)

    async def fetch_tree(self) -> CacheEntry:
        url = (
            f"{GITHUB_API}/repos/{self.owner}/{self.repo}/git/trees/"
            f"{self.ref}?recursive=1"
        )

        def transform(d: dict) -> dict:
            entries = d.get("tree") or []
            nodes = [
                {
                    "path": e.get("path"),
                    "type": e.get("type"),  # "blob" or "tree"
                    "size": e.get("size"),
                }
                for e in entries[:TREE_LIMIT]
            ]
            return {
                "sha": d.get("sha"),
                "truncated": bool(d.get("truncated")) or len(entries) > TREE_LIMIT,
                "count": len(entries),
                "tree": nodes,
            }

        return await self._fetch("tree", url, TTL_TREE, transform)

    async def overview(self) -> dict:
        repo, commits, tree = await asyncio.gather(
            self.fetch_repo(), self.fetch_commits(), self.fetch_tree()
        )
        errors = [e.error for e in (repo, commits, tree) if e.error]
        return {
            "repo": {"owner": self.owner, "name": self.repo, "ref": self.ref},
            "info": repo.data,
            "commits": commits.data or [],
            "latest_commit": (commits.data or [None])[0],
            "tree": tree.data,
            "rate_limit": self.rate,
            "errors": errors,
            "fetched_at": time.time(),
        }
