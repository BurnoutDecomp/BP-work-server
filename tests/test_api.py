from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from urllib.parse import quote

import bp_work_server.api as api
from bp_work_server.api import create_app
from bp_work_server.store import WorkStore, iso


@pytest.fixture(autouse=True)
def _disable_enforcement(monkeypatch):
    # These tests exercise API/dashboard behavior, not auth (auth lives in test_auth.py).
    # Run them with token enforcement off so body `agent` is used directly. Admin
    # endpoints still require an admin worker id regardless of this switch.
    monkeypatch.setenv("BP_WORK_REQUIRE_TOKEN", "0")


def make_client(tmp_path) -> tuple[TestClient, WorkStore]:
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
    return TestClient(create_app(store)), store


def test_dashboard_page_and_state(tmp_path):
    client, _ = make_client(tmp_path)

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
    client, _ = make_client(tmp_path)

    response = client.post(
        "/claims",
        json={"tu": "GameSource/B.cpp", "agent": "agent-a", "lease_seconds": 7200},
    )
    state = client.get("/dashboard/state").json()

    assert response.status_code == 201
    assert state["counts"]["in_progress"] == 1
    assert state["agents"][0]["name"] == "agent-a"
    assert state["active_work"][0]["id"] == "GameSource/B.cpp"


def test_finished_owner_is_not_an_active_agent(tmp_path):
    """A done/blocked TU may retain a stale owner (the durable status.json mirror
    records it). Such an agent holds no live work and must not appear as an active
    agent or inflate the agent count on the dashboard."""
    client, store = make_client(tmp_path)
    with store.connect() as con:
        con.execute(
            "UPDATE tu SET status='done', owner='ghost' WHERE id='GameSource/A.cpp'"
        )

    # 'live' has real in-progress work; 'ghost' only owns a finished TU.
    client.post("/claims", json={"tu": "GameSource/B.cpp", "agent": "live"})
    state = client.get("/dashboard/state").json()

    names = [a["name"] for a in state["agents"]]
    assert names == ["live"]
    assert all(a["in_progress"] + a["compiled"] > 0 for a in state["agents"])


def test_path_encoded_tu_status_endpoints(tmp_path):
    client, _ = make_client(tmp_path)
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


def test_explorer_search_filter_and_detail(tmp_path):
    client, _ = make_client(tmp_path)

    facets = client.get("/api/facets").json()
    assert "decfigs" in facets["sources"]
    assert "todo" in facets["tu_statuses"]

    # text + status filter
    search = client.get("/api/tus", params={"q": "A.cpp", "status": ["todo"]}).json()
    assert search["total"] == 1
    assert search["items"][0]["id"] == "GameSource/A.cpp"

    # function search
    funcs = client.get("/api/funcs", params={"q": "Run"}).json()
    assert funcs["total"] == 2

    # TU detail exposes the data handed to agents
    detail = client.get("/api/tu", params={"id": "GameSource/A.cpp"}).json()
    assert detail["n_funcs"] == 2
    assert [f["name"] for f in detail["funcs"]] == ["A::Run"]

    missing = client.get("/api/tu", params={"id": "GameSource/missing.cpp"})
    assert missing.status_code == 404


def test_admin_endpoints_require_admin_role(tmp_path):
    client, store = make_client(tmp_path)
    user = store.create_worker("regular")

    # no id -> 401
    assert client.post("/admin/import?workflow_root=missing").status_code == 401
    # a non-admin id -> 403
    assert client.post(
        "/admin/import?workflow_root=missing",
        headers={"X-Work-Token": user["token"]},
    ).status_code == 403


def test_admin_sync_calls_fixed_server_side_sync(tmp_path, monkeypatch):
    client, store = make_client(tmp_path)
    admin = store.create_worker("boss", is_admin=True)

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
        headers={"X-Work-Token": admin["token"]},
        json={"branch": "main", "commit": "abc123", "reset": False},
    )

    assert response.status_code == 200
    assert response.json()["commit"] == "abc123"
