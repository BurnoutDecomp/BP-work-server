from __future__ import annotations

import asyncio
import os
import subprocess

import httpx

from bp_work_server.github import GitHubClient, login_from_noreply_email


def test_login_from_noreply_email():
    assert login_from_noreply_email("76881633+Adriwin06@users.noreply.github.com") == "Adriwin06"
    assert login_from_noreply_email("HumanGamer@users.noreply.github.com") == "HumanGamer"
    # Plain emails carry no login; the caller falls back to the API map.
    assert login_from_noreply_email("jebcraftserver@gmail.com") is None
    assert login_from_noreply_email(None) is None


def test_github_overview_uses_cache_and_transforms_payloads():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        path = request.url.path
        if path.endswith("/commits"):
            return httpx.Response(
                200,
                headers={"ETag": '"commits"', "X-RateLimit-Remaining": "59", "X-RateLimit-Limit": "60"},
                json=[
                    {
                        "sha": "abcdef123456",
                        "html_url": "https://example.test/commit",
                        "author": {"login": "octo", "avatar_url": "https://example.test/a.png"},
                        "commit": {
                            "message": "Reconstruct TU\n\nBody",
                            "author": {"name": "Ada", "date": "2026-06-14T12:00:00Z"},
                        },
                    }
                ],
            )
        if "/git/trees/" in path:
            return httpx.Response(
                200,
                headers={"ETag": '"tree"', "X-RateLimit-Remaining": "58", "X-RateLimit-Limit": "60"},
                json={"sha": "tree-sha", "truncated": False, "tree": [{"path": "src/foo.cpp", "type": "blob", "size": 42}]},
            )
        return httpx.Response(
            200,
            headers={"ETag": '"repo"', "X-RateLimit-Remaining": "57", "X-RateLimit-Limit": "60"},
            json={
                "full_name": "owner/repo",
                "description": "repo description",
                "html_url": "https://example.test/repo",
                "default_branch": "dev",
                "stargazers_count": 1,
                "forks_count": 2,
                "open_issues_count": 3,
                "watchers_count": 4,
                "language": "C++",
                "pushed_at": "2026-06-14T12:00:00Z",
                "license": {"spdx_id": "MIT"},
            },
        )

    async def run():
        client = GitHubClient(owner="owner", repo="repo", ref="dev")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.github.com",
        )
        try:
            first = await client.overview()
            second = await client.overview()
        finally:
            await client.aclose()
        return first, second

    first, second = asyncio.run(run())

    assert first["info"]["full_name"] == "owner/repo"
    assert first["latest_commit"]["short_sha"] == "abcdef1"
    assert first["latest_commit"]["message"] == "Reconstruct TU"
    assert first["tree"]["tree"][0] == {"path": "src/foo.cpp", "type": "blob", "size": 42}
    assert second["latest_commit"]["short_sha"] == "abcdef1"
    assert len(calls) == 3


def test_github_overview_falls_back_to_local_clone_on_rate_limit(tmp_path, monkeypatch):
    repo = tmp_path / "b5-decomp"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "dev"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Local Dev"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-06-17T02:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-06-17T02:00:00+00:00",
    }
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "Local commit"],
        check=True,
        env=env,
    )
    monkeypatch.setenv("BP_DECOMP_ROOT", str(repo))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "60"},
            json={"message": "rate limited"},
        )

    async def run():
        client = GitHubClient(owner="owner", repo="repo", ref="dev")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.github.com",
        )
        try:
            return await client.overview()
        finally:
            await client.aclose()

    overview = asyncio.run(run())

    assert overview["info"]["full_name"] == "owner/repo"
    assert overview["latest_commit"]["message"] == "Local commit"
    assert overview["latest_commit"]["author"] == "Local Dev"
    tree_nodes = overview["tree"]["tree"]
    assert {"path": "src/foo.cpp", "type": "blob", "size": 7} in tree_nodes
    # The folder skeleton is listed too (via ls-tree -t), so folders never vanish.
    assert {"path": "src", "type": "tree", "size": None} in tree_nodes
    assert any("local b5-decomp clone" in error for error in overview["errors"])
