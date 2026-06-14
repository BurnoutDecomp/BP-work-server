from __future__ import annotations

from fastapi.testclient import TestClient

from bp_work_server.api import create_app
from bp_work_server.store import WorkStore, iso


def make_store(tmp_path) -> WorkStore:
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    with store.connect() as con:
        con.execute(
            "INSERT INTO tu(id, source, status, n_funcs, n_decfigs, dest_path, updated_at) "
            "VALUES('GameSource/A.cpp', 'decfigs', 'todo', 1, 1, NULL, ?)",
            (iso(),),
        )
    return store


def test_token_enforced_by_default_and_records_username(tmp_path):
    store = make_store(tmp_path)
    user = store.create_worker("JeBobs")  # direct-DB bootstrap (a regular user)
    client = TestClient(create_app(store))

    # no token -> rejected (enforcement is on by default, no env needed)
    assert client.post("/claims", json={"tu": "GameSource/A.cpp", "agent": "x"}).status_code == 401

    # valid token -> claimed, and the USERNAME is recorded as owner (not the token)
    ok = client.post(
        "/claims",
        json={"tu": "GameSource/A.cpp", "agent": "spoofed"},
        headers={"X-Work-Token": user["token"]},
    )
    assert ok.status_code == 201
    assert ok.json()["owner"] == "JeBobs"

    # invalid token -> rejected
    bad = client.post(
        "/claims/next", json={"agent": "x", "n": 1}, headers={"X-Work-Token": "nope"}
    )
    assert bad.status_code == 401


def test_admin_is_a_role_not_a_shared_secret(tmp_path):
    store = make_store(tmp_path)
    admin = store.create_worker("Adriwin", is_admin=True)
    user = store.create_worker("JeBobs")
    client = TestClient(create_app(store))

    # a regular id cannot reach admin endpoints -> 403
    forbidden = client.post(
        "/admin/workers", json={"username": "Mallory"}, headers={"X-Work-Token": user["token"]}
    )
    assert forbidden.status_code == 403

    # no id at all -> 401
    assert client.get("/admin/workers").status_code == 401

    # the admin id can mint and list
    minted = client.post(
        "/admin/workers",
        json={"username": "Carol", "is_admin": False},
        headers={"X-Work-Token": admin["token"]},
    )
    assert minted.status_code == 201
    listed = client.get("/admin/workers", headers={"X-Work-Token": admin["token"]})
    assert listed.status_code == 200
    names = {w["username"]: w["is_admin"] for w in listed.json()["workers"]}
    assert names["Adriwin"] is True and names["JeBobs"] is False and names["Carol"] is False

    # revoke the user id; it then fails auth
    assert client.delete(
        f"/admin/workers/{user['token']}", headers={"X-Work-Token": admin["token"]}
    ).status_code == 204
    revoked = client.post(
        "/claims", json={"tu": "GameSource/A.cpp", "agent": "x"},
        headers={"X-Work-Token": user["token"]},
    )
    assert revoked.status_code == 401


def test_enforcement_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_WORK_REQUIRE_TOKEN", "0")
    store = make_store(tmp_path)
    client = TestClient(create_app(store))

    # with enforcement off, the body agent is trusted (private/solo deployment)
    ok = client.post("/claims", json={"tu": "GameSource/A.cpp", "agent": "solo"})
    assert ok.status_code == 201
    assert ok.json()["owner"] == "solo"


def test_workers_migrate_from_legacy_work_db_to_users_db(tmp_path):
    db_path = tmp_path / "work.sqlite3"
    users_db_path = tmp_path / "workers.sqlite3"
    legacy = WorkStore(db_path)
    legacy.migrate()
    admin = legacy.create_worker("Adriwin", is_admin=True)

    # Simulate the old single-DB layout that exists on the deployed server today.
    users_db_path.unlink(missing_ok=True)
    with legacy.connect() as con:
        con.executescript(
            """
            CREATE TABLE worker(
              token TEXT PRIMARY KEY,
              username TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              is_admin INTEGER NOT NULL DEFAULT 0,
              created_at TEXT,
              last_seen TEXT
            );
            """
        )
        con.execute(
            """
            INSERT INTO worker(token, username, active, is_admin, created_at, last_seen)
            VALUES(?, 'Adriwin', 1, 1, ?, NULL)
            """,
            (admin["token"], iso()),
        )

    migrated = WorkStore(db_path, users_db_path)
    migrated.migrate()

    assert migrated.resolve_admin(admin["token"]) == "Adriwin"
    assert users_db_path.exists()
    with migrated.connect() as con:
        assert not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker'"
        ).fetchone()
