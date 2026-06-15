from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class WorkServerError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"work server HTTP {status}: {message}")
        self.status = status
        self.message = message


@dataclass
class WorkServerClient:
    base_url: str
    timeout: float = 30.0
    token: str | None = None  # X-Work-Token (server-issued worker id; admin role = admin id)

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def create_worker(self, username: str, is_admin: bool = False) -> dict[str, Any]:
        return self._request(
            "POST", "/admin/workers", {"username": username, "is_admin": is_admin}
        )

    def list_workers(self) -> dict[str, Any]:
        return self._request("GET", "/admin/workers")

    def revoke_worker(self, token: str) -> dict[str, Any]:
        return self._request("DELETE", f"/admin/workers/{self._path(token)}")

    def next(self, n: int = 1, goal: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"n": n}
        if goal:
            params["goal"] = goal
        return self._request("GET", "/next?" + urlencode(params))

    def claim(
        self,
        tu: str,
        agent: str,
        lease_seconds: int = 7200,
        force: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/claims",
            {"tu": tu, "agent": agent, "lease_seconds": lease_seconds, "force": force},
        )

    def claim_next(
        self,
        agent: str,
        n: int = 1,
        lease_seconds: int = 7200,
        goal: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"agent": agent, "n": n, "lease_seconds": lease_seconds}
        if goal:
            body["goal"] = goal
        return self._request("POST", "/claims/next", body)

    def heartbeat(self, tu: str, agent: str, lease_seconds: int = 7200) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/claims/{self._path(tu)}/heartbeat",
            {"agent": agent, "lease_seconds": lease_seconds},
        )

    def compiled(
        self,
        tu: str,
        agent: str,
        notes: str | None = None,
        commit: str | None = None,
        files: list[str] | None = None,
    ) -> None:
        self._request(
            "POST",
            f"/tu/{self._path(tu)}/compiled",
            {"agent": agent, "notes": notes, "commit": commit, "files": files or []},
        )

    def review(
        self,
        tu: str,
        agent: str,
        verdict: str,
        notes: str | None = None,
        commit: str | None = None,
    ) -> None:
        self._request(
            "POST",
            f"/tu/{self._path(tu)}/review",
            {"agent": agent, "verdict": verdict, "notes": notes, "commit": commit},
        )

    def block(self, tu: str, agent: str, reason: str) -> None:
        self._request(
            "POST",
            f"/tu/{self._path(tu)}/block",
            {"agent": agent, "reason": reason},
        )

    def snapshot(self, include_tus: bool = True) -> dict[str, Any]:
        return self._request("GET", "/snapshot?" + urlencode({"include_tus": str(include_tus).lower()}))

    def export_status(self) -> dict[str, Any]:
        """The committed status.json regenerated from the live DB (durable done/blocked
        + func statuses). Used by CI to refresh git without a worker pushing by hand."""
        return self._request("GET", "/export/status")

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Work-Token"] = self.token
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                if not payload:
                    return {}
                return json.loads(payload.decode("utf-8"))
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise WorkServerError(exc.code, message) from exc

    def _path(self, value: str) -> str:
        from urllib.parse import quote

        return quote(value, safe="")
