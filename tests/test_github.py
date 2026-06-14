from __future__ import annotations

import asyncio

import httpx

from bp_work_server.github import GitHubClient


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
