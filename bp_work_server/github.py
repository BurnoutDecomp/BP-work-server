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
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from bp_work_server.decomp import DEFAULT_DECOMP_ROOT

GITHUB_API = "https://api.github.com"

# Repository the dashboard mirrors. Overridable via environment for forks.
REPO_OWNER = os.environ.get("BP_GITHUB_OWNER", "BurnoutDecomp")
REPO_NAME = os.environ.get("BP_GITHUB_REPO", "b5-decomp")
REPO_REF = os.environ.get("BP_GITHUB_REF", "dev")

# How long a cached resource is served before we revalidate upstream (seconds).
TTL_REPO = 300
TTL_COMMITS = 180
TTL_TREE = 600
# Author identities barely change, so the email -> GitHub-login map is cached an
# hour; a few pages cover every recent contributor.
TTL_AUTHORS = 3_600
AUTHOR_MAP_PAGES = 3


def login_from_noreply_email(email: str | None) -> str | None:
    """Extract a GitHub login from a ``...noreply.github.com`` commit email.

    These embed the login (``12345+login@`` or ``login@``); other emails return
    None and must be resolved via the API author map instead.
    """
    if not email or "users.noreply.github.com" not in email.lower():
        return None
    local = email.split("@", 1)[0]
    return local.split("+", 1)[-1] or None

# Cap the tree we ship to the browser; the full recursive tree can be huge.
TREE_LIMIT = 8000
_FIELD_SEP = "\x1f"


def _cap_tree_nodes(nodes: list[dict]) -> tuple[list[dict], bool]:
    """Truncate a flat recursive tree to TREE_LIMIT without ever dropping a
    directory. GitHub returns entries in path-sorted order, so a naive
    ``nodes[:LIMIT]`` slice silently deletes whole subtrees that sort late
    (folders vanish from the dashboard). Keeping every ``tree`` entry preserves
    the folder skeleton; blobs fill the remaining budget."""
    if len(nodes) <= TREE_LIMIT:
        return nodes, False
    dirs = [n for n in nodes if n.get("type") == "tree"]
    blobs = [n for n in nodes if n.get("type") != "tree"]
    budget = max(0, TREE_LIMIT - len(dirs))
    return dirs + blobs[:budget], len(blobs) > budget


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

    @property
    def local_root(self) -> Path:
        return Path(os.environ.get("BP_DECOMP_ROOT", DEFAULT_DECOMP_ROOT))

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

    async def _fetch_author_page(self, page: int, per_page: int) -> CacheEntry:
        url = (
            f"{GITHUB_API}/repos/{self.owner}/{self.repo}/commits"
            f"?sha={self.ref}&per_page={per_page}&page={page}"
        )

        def transform(items: list[dict]) -> list[tuple[str, str]]:
            pairs = []
            for c in items:
                email = ((c.get("commit") or {}).get("author") or {}).get("email")
                login = (c.get("author") or {}).get("login")
                if email and login:
                    pairs.append((email.lower(), login))
            return pairs

        return await self._fetch(
            f"authors:{self.ref}:{page}:{per_page}", url, TTL_AUTHORS, transform
        )

    async def author_login_map(self, pages: int = AUTHOR_MAP_PAGES) -> dict[str, str]:
        """Map commit-author email -> GitHub login from recent commits.

        Lets reconstructed events show the GitHub identity the dashboard uses
        (e.g. ``JeBobs``) rather than the raw git author name (``Nathan V.``).
        Best-effort: an empty map on failure just falls back to the git name.
        """
        mapping: dict[str, str] = {}
        for page in range(1, pages + 1):
            entry = await self._fetch_author_page(page, 100)
            for email, login in entry.data or []:
                mapping.setdefault(email, login)
        return mapping

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
                for e in entries
            ]
            kept, capped = _cap_tree_nodes(nodes)
            return {
                "sha": d.get("sha"),
                "truncated": bool(d.get("truncated")) or capped,
                "count": len(entries),
                "tree": kept,
            }

        return await self._fetch("tree", url, TTL_TREE, transform)

    def _local_git(self, *args: str) -> str | None:
        root = self.local_root
        if not (root / ".git").exists():
            return None
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def _local_repo_info(self) -> dict:
        return {
            "full_name": f"{self.owner}/{self.repo}",
            "description": "Local b5-decomp clone",
            "html_url": f"https://github.com/{self.owner}/{self.repo}",
            "default_branch": self.ref,
            "stargazers_count": 0,
            "forks_count": 0,
            "open_issues_count": 0,
            "watchers_count": 0,
            "language": "C++",
            "pushed_at": self._local_latest_date(),
            "license": None,
        }

    def _local_latest_date(self) -> str | None:
        out = self._local_git("log", "-1", "--format=%cI")
        return out.strip() if out and out.strip() else None

    def _local_commits(self, count: int = 8) -> list[dict]:
        out = self._local_git(
            "log",
            f"-{count}",
            f"--format=%H{_FIELD_SEP}%s{_FIELD_SEP}%an{_FIELD_SEP}%aI",
        )
        commits = []
        for line in (out or "").splitlines():
            sha, _, rest = line.partition(_FIELD_SEP)
            message, _, rest = rest.partition(_FIELD_SEP)
            author, _, date = rest.partition(_FIELD_SEP)
            sha = sha.strip()
            if not sha:
                continue
            commits.append(
                {
                    "sha": sha,
                    "short_sha": sha[:7],
                    "message": message.strip(),
                    "author": author.strip() or None,
                    "login": None,
                    "avatar_url": None,
                    "date": date.strip() or None,
                    "html_url": f"https://github.com/{self.owner}/{self.repo}/commit/{sha}",
                    "local_fallback": True,
                }
            )
        return commits

    def _local_tree(self) -> dict | None:
        # -t lists directory (tree) entries too, so the folder skeleton matches
        # what the GitHub API returns and _cap_tree_nodes can preserve it.
        out = self._local_git("ls-tree", "-r", "-t", "--long", "HEAD")
        if out is None:
            return None
        nodes = []
        for line in out.splitlines():
            meta, _, path = line.partition("\t")
            if not path:
                continue
            parts = meta.split()
            kind = parts[1] if len(parts) > 1 else "blob"
            size = None
            if len(parts) > 3 and parts[3].isdigit():
                size = int(parts[3])
            nodes.append({"path": path, "type": kind, "size": size})
        kept, capped = _cap_tree_nodes(nodes)
        return {
            "sha": None,
            "truncated": capped,
            "count": len(nodes),
            "tree": kept,
            "local_fallback": True,
        }

    async def overview(self) -> dict:
        repo, commits, tree = await asyncio.gather(
            self.fetch_repo(), self.fetch_commits(), self.fetch_tree()
        )
        local_commits = commits.data or await asyncio.to_thread(self._local_commits)
        local_tree = tree.data or await asyncio.to_thread(self._local_tree)
        repo_info = repo.data or await asyncio.to_thread(self._local_repo_info)
        errors = [e.error for e in (repo, commits, tree) if e.error]
        if not commits.data and local_commits:
            errors.append("github commits unavailable; serving local b5-decomp clone")
        if not tree.data and local_tree:
            errors.append("github tree unavailable; serving local b5-decomp clone")
        if not repo.data and repo_info:
            errors.append("github repo metadata unavailable; serving local b5-decomp clone")
        return {
            "repo": {"owner": self.owner, "name": self.repo, "ref": self.ref},
            "info": repo_info,
            "commits": local_commits or [],
            "latest_commit": (local_commits or [None])[0],
            "tree": local_tree,
            "rate_limit": self.rate,
            "errors": errors,
            "fetched_at": time.time(),
        }
