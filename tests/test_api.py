from __future__ import annotations

from fastapi.testclient import TestClient
from urllib.parse import quote

import bp_work_server.api as api
from bp_work_server.api import create_app
from bp_work_server.store import WorkStore, iso


def make_client(tmp_path) -> TestClient:
    store = WorkStore(tmp_path / "api.sqlite3")
    store.migrate()
    with store.connect() as con:
        con.execute(
            """
            INSERT INTO tu(id, source, status, n_funcs, n_decfigs, dest_path, updated_at)
            VALUES
              ('GameSource/A.cpp', 'decfigs', 'todo', 2, 2, 'b5-decomp/src/GameSource/A.cpp', ?),
              ('GameSource/B.cpp', 'decfigs', 'todo', 1, 1, 'b5-decomp/src/GameSource/B.cpp', ?)
            """,
            (iso(), iso()),
        )
        con.execute("INSERT INTO func(name, tu_id) VALUES('A::Run', 'GameSource/A.cpp')")
        con.execute("INSERT INTO func(name, tu_id) VALUES('B::Run', 'GameSource/B.cpp')")
    return TestClient(create_app(store))


def test_dashboard_page_and_state(tmp_path):
    client = make_client(tmp_path)

    page = client.get("/")
    state = client.get("/dashboard/state")

    assert page.status_code == 200
    assert "BP Decomp Progress" in page.text
    assert state.status_code == 200
    body = state.json()
    assert body["totals"]["tus"] == 2
    assert body["counts"]["todo"] == 2
    assert body["next"]["items"]


def test_claim_updates_dashboard_agents(tmp_path):
    client = make_client(tmp_path)

    response = client.post(
        "/claims",
        json={"tu": "GameSource/B.cpp", "agent": "agent-a", "lease_seconds": 7200},
    )
    state = client.get("/dashboard/state").json()

    assert response.status_code == 201
    assert state["counts"]["in_progress"] == 1
    assert state["agents"][0]["name"] == "agent-a"
    assert state["active_work"][0]["id"] == "GameSource/B.cpp"


def test_path_encoded_tu_status_endpoints(tmp_path):
    client = make_client(tmp_path)
    tu = "GameSource/B.cpp"
    encoded = quote(tu, safe="")

    claim = client.post(
        "/claims",
        json={"tu": tu, "agent": "agent-a", "lease_seconds": 7200},
    )
    compiled = client.post(
        f"/tu/{encoded}/compiled",
        json={"agent": "agent-a", "notes": "compiled", "files": []},
    )
    review = client.post(
        f"/tu/{encoded}/review",
        json={"agent": "agent-a", "verdict": "pass", "notes": "gate-only", "files": []},
    )
    state = client.get("/dashboard/state").json()

    assert claim.status_code == 201
    assert compiled.status_code == 204
    assert review.status_code == 204
    assert state["counts"]["done"] == 1


def test_admin_import_requires_token_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_WORK_ADMIN_TOKEN", "secret-token")
    client = make_client(tmp_path)

    missing = client.post("/admin/import?workflow_root=missing")
    wrong = client.post(
        "/admin/import?workflow_root=missing",
        headers={"X-BP-Admin-Token": "wrong"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_admin_sync_calls_fixed_server_side_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_WORK_ADMIN_TOKEN", "secret-token")
    client = make_client(tmp_path)

    def fake_sync(store, branch=None, reset=False):
        assert branch == "main"
        assert reset is False
        return {
            "tus": 2,
            "funcs": 2,
            "deps": 0,
            "goals": 0,
            "status_rows": 0,
            "repo_url": "https://example.test/repo.git",
            "workflow_root": str(tmp_path / "workflow"),
            "branch": branch,
            "commit": "abc123",
        }

    monkeypatch.setattr(api, "sync_workflow_repo", fake_sync)
    response = client.post(
        "/admin/sync",
        headers={"X-BP-Admin-Token": "secret-token"},
        json={"branch": "main", "commit": "abc123", "reset": False},
    )

    assert response.status_code == 200
    assert response.json()["commit"] == "abc123"
