import json

import pytest
from fastapi.testclient import TestClient

from bp_work_server import source_status
from bp_work_server.api import create_app
from bp_work_server.store import WorkStore, iso


@pytest.fixture
def store(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    with store.connect() as con:
        for name, status, owner in (("A", "todo", None), ("Claim", "in_progress", "worker"),
                                    ("Compiled", "compiled", "worker"), ("Review", "blocked", None)):
            con.execute("INSERT INTO tu(id,status,owner,notes,updated_at) VALUES(?,?,?,?,?)",
                        (name, status, owner, "semantic bug remains" if name == "Review" else None, iso()))
            con.execute("INSERT INTO func(name,tu_id) VALUES(?,?)", (name + "::Run", name))
    return store


def evidence(commit="a", base=None):
    names = ("A", "Claim", "Compiled", "Review")
    return {"version": 1, "source_commit": commit * 40, "base_source_commit": base * 40 if base else None,
            "functions": {name + "::Run": {"digest": "d" * 64, "file": name + ".cpp", "line": 1} for name in names},
            "tus": {name: {"digest": "e" * 64, "functions": [name + "::Run"]} for name in names}}


def rows(store):
    with store.connect() as con:
        return ({r["id"]: dict(r) for r in con.execute("SELECT * FROM tu")},
                {r["name"]: r["status"] for r in con.execute("SELECT * FROM func")})


def test_source_recovery_preserves_live_work_and_semantic_blocks(store):
    before, _ = rows(store)
    result = source_status.apply(store, evidence())
    after, funcs = rows(store)
    assert result["tus_completed"] == 1
    assert after["A"]["status"] == "done" and funcs["A::Run"] == "recovered"
    assert after["Claim"] == before["Claim"] and after["Compiled"] == before["Compiled"]
    assert funcs["Claim::Run"] == funcs["Compiled::Run"] == "todo"
    assert after["Review"]["status"] == "blocked"
    assert source_status.apply(store, evidence())["unchanged"] is True
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM event WHERE action='reconcile'").fetchone()[0] == 1


def test_removed_automatic_bodies_reopen_only_automatic_records(store):
    source_status.apply(store, evidence())
    with store.connect() as con:
        con.execute("UPDATE func SET status='reviewed' WHERE name='Review::Run'")
    removed = evidence("b", "a")
    removed.update(functions={}, tus={})
    result = source_status.apply(store, removed)
    tus, funcs = rows(store)
    assert tus["A"]["status"] == "blocked" and funcs["A::Run"] == "todo"
    assert funcs["Review::Run"] == "reviewed"
    assert result["tus_reopened"] == result["functions_removed"] == 1


def test_manual_reset_is_respected_on_unchanged_source(store):
    source_status.apply(store, evidence())
    with store.connect() as con:
        con.execute("UPDATE tu SET status='todo',notes='manual reset' WHERE id='A'")
        con.execute("UPDATE func SET status='todo' WHERE name='A::Run'")
    source_status.apply(store, evidence("b", "a"))
    tus, funcs = rows(store)
    assert tus["A"]["status"] == "todo" and funcs["A::Run"] == "todo"


def test_stale_or_incomplete_evidence_rolls_back(store):
    source_status.apply(store, evidence())
    before = rows(store)
    with pytest.raises(ValueError, match="advanced"):
        source_status.apply(store, evidence("b"))
    assert rows(store) == before
    bad = evidence("b", "a")
    with store.connect() as con:
        con.execute("INSERT INTO func(name,tu_id) VALUES('A::Other','A')")
    with pytest.raises(ValueError, match="membership"):
        source_status.apply(store, bad)
    assert rows(store)[1]["A::Other"] == "todo"


def test_ordinary_sync_cannot_overwrite_live_claims_or_compiled_work(store):
    before = rows(store)
    with store.connect() as con:
        store._restore_status(con, {
            "tu": {name: {"status": "done"} for name in ("Claim", "Compiled")},
            "func": {name + "::Run": {"status": "reviewed"} for name in ("Claim", "Compiled")},
        })
    assert rows(store) == before


def test_ordinary_sync_cannot_downgrade_a_later_review(store):
    with store.connect() as con:
        con.execute("UPDATE func SET status='reviewed' WHERE name='A::Run'")
        store._restore_status(con, {"func": {"A::Run": {"status": "recovered"}}})
    assert rows(store)[1]["A::Run"] == "reviewed"


def test_new_complete_source_can_clear_a_reconstruction_gap(store):
    with store.connect() as con:
        con.execute("UPDATE tu SET status='blocked',notes='missing reconstructed bodies' WHERE id='A'")
    source_status.apply(store, evidence())
    assert rows(store)[0]["A"]["status"] == "done"


def test_source_status_endpoint_requires_admin_and_reports_revision(store):
    client = TestClient(create_app(store))
    admin = store.create_worker("publisher", is_admin=True)
    assert client.post("/admin/source-status", json=evidence()).status_code == 401
    response = client.post("/admin/source-status", json=evidence(), headers={"X-Work-Token": admin["token"]})
    assert response.status_code == 200
    assert client.get("/api/source-status").json()["source_commit"] == "a" * 40
    assert client.get("/dashboard/state").json()["ledger_evidence"]["source_commit"] == "a" * 40
    conflict = client.post("/admin/source-status", json=evidence("b"), headers={"X-Work-Token": admin["token"]})
    assert conflict.status_code == 409


def test_metadata_import_adds_membership_without_replaying_old_statuses(store, tmp_path):
    progress = tmp_path / "workflow" / "progress"
    progress.mkdir(parents=True)
    names = ("A", "Claim", "Compiled", "Review", "New")
    index = {name: {"source": "decfigs", "functions": [name + "::Run"], "n_funcs": 1} for name in names}
    (progress / "tu_index.json").write_text(json.dumps(index))
    (progress / "status.json").write_text(json.dumps({
        "tu": {"A": {"status": "blocked"}}, "func": {"A::Run": {"status": "todo"}}}))
    with store.connect() as con:
        con.execute("UPDATE tu SET status='done' WHERE id='A'")
        con.execute("UPDATE func SET status='reviewed' WHERE name='A::Run'")
    store.import_workflow(progress.parent, restore_status=False)
    tus, funcs = rows(store)
    assert tus["A"]["status"] == "done" and funcs["A::Run"] == "reviewed"
    assert tus["New"]["status"] == "todo"
    assert tus["Claim"]["status"] == "in_progress"
