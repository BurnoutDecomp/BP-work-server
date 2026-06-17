from __future__ import annotations

import json
from datetime import timedelta

import pytest

from bp_work_server.store import WorkStore, iso, utcnow


def make_store(tmp_path) -> WorkStore:
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    with store.connect() as con:
        con.execute(
            """
            INSERT INTO tu(id, source, status, n_funcs, n_decfigs, dest_path, updated_at)
            VALUES
              ('GameSource/A.cpp', 'decfigs', 'todo', 2, 2, 'b5-decomp/src/GameSource/A.cpp', ?),
              ('GameSource/B.cpp', 'decfigs', 'todo', 1, 1, 'b5-decomp/src/GameSource/B.cpp', ?),
              ('class:Utility', 'class', 'todo', 1, 0, NULL, ?)
            """,
            (iso(), iso(), iso()),
        )
        con.execute("INSERT INTO func(name, tu_id) VALUES('A::Run', 'GameSource/A.cpp')")
        con.execute("INSERT INTO func(name, tu_id) VALUES('A::Stop', 'GameSource/A.cpp')")
        con.execute("INSERT INTO func(name, tu_id) VALUES('B::Run', 'GameSource/B.cpp')")
        con.execute("INSERT INTO func(name, tu_id) VALUES('Utility::Fn', 'class:Utility')")
        con.execute(
            "INSERT INTO tu_dep(tu_id, dep_id, weight) VALUES('GameSource/A.cpp', 'GameSource/B.cpp', 1)"
        )
    return store


def test_backfilled_event_targets_maps_tu_to_dest(tmp_path):
    store = make_store(tmp_path)
    with store.connect() as con:
        # Two backfilled events (one per source) plus a normal one that must not appear.
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "GameSource/A.cpp", "JeBobs", "review_pass",
             json.dumps({"source": "workflow commit delta"})),
        )
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "GameSource/B.cpp", "Adriwin", "review_pass",
             json.dumps({"source": "legacy pre-server attribution"})),
        )
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "class:Utility", "live", "claim", json.dumps({"lease_seconds": 60})),
        )

    targets = store.backfilled_event_targets()

    assert targets == {
        "GameSource/A.cpp": "b5-decomp/src/GameSource/A.cpp",
        "GameSource/B.cpp": "b5-decomp/src/GameSource/B.cpp",
    }


def test_backfilled_event_targets_skip_tus_with_reliable_events(tmp_path):
    store = make_store(tmp_path)
    with store.connect() as con:
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "GameSource/A.cpp", "agent", "review_pass",
             json.dumps({"source": "workflow commit delta"})),
        )
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "GameSource/A.cpp", "Adriwin", "claim", json.dumps({"lease_seconds": 60})),
        )

    assert store.backfilled_event_targets() == {}
    assert all(
        event["detail"].get("source") != "workflow commit delta"
        for event in store.dashboard_state()["recent_events"]
    )


def test_dashboard_keeps_source_less_workflow_events(tmp_path):
    store = make_store(tmp_path)
    with store.connect() as con:
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "GameSource/A.cpp", "Derneuere", "claim",
             json.dumps({"force": False, "lease_seconds": 7200})),
        )
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "GameSource/A.cpp", "Derneuere", "compiled",
             json.dumps({"commit": None, "notes": None})),
        )

    actions = [event["action"] for event in store.dashboard_state()["recent_events"]]

    assert "claim" in actions
    assert "compiled" in actions


def test_actor_maps_canonicalize_github_and_case_aliases(tmp_path):
    store = make_store(tmp_path)
    store.create_worker("Adriwin", github_username="adriwin06")
    store.create_worker("Derneuere")
    with store.connect() as con:
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "GameSource/A.cpp", "adriwin06", "review_pass", "{}"),
        )
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "GameSource/B.cpp", "derneuere", "review_pass", "{}"),
        )

    state = store.dashboard_state()

    names = {agent["name"] for agent in state["agents"]}
    assert "Adriwin" in names
    assert "adriwin06" not in names
    assert "Derneuere" in names
    assert "derneuere" not in names
    assert {event["agent"] for event in state["recent_events"]} == {"Adriwin", "Derneuere"}


def test_dashboard_hides_lease_housekeeping_events(tmp_path):
    store = make_store(tmp_path)
    with store.connect() as con:
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "GameSource/A.cpp", "agent", "lease_missing", "{}"),
        )

    state = store.dashboard_state()

    assert all(event["action"] != "lease_missing" for event in state["recent_events"])
    detail = store.tu_detail("GameSource/A.cpp")
    assert detail["last_actor"] is None


def test_next_is_dependency_ranked(tmp_path):
    store = make_store(tmp_path)

    _goal, rows = store.next_tus(n=3)

    assert [row.id for row in rows] == [
        "GameSource/B.cpp",
        "class:Utility",
        "GameSource/A.cpp",
    ]
    assert rows[0].unresolved_deps == 0
    assert rows[2].unresolved_deps == 1


def test_claim_conflict_and_owner_enforcement(tmp_path):
    store = make_store(tmp_path)

    first = store.claim("GameSource/B.cpp", "agent-a")
    second = store.claim("GameSource/B.cpp", "agent-b")

    assert first.claimed is True
    assert second.claimed is False
    assert second.owner == "agent-a"

    with pytest.raises(PermissionError):
        store.mark_compiled("GameSource/B.cpp", "agent-b")

    store.mark_compiled("GameSource/B.cpp", "agent-a", notes="compile passed")
    _active_goal, counts, tus = store.snapshot()
    by_id = {tu.id: tu for tu in tus}
    assert counts.compiled == 1
    assert by_id["GameSource/B.cpp"].status == "compiled"


def test_review_pass_unblocks_dependents(tmp_path):
    store = make_store(tmp_path)
    store.claim("GameSource/B.cpp", "agent-a")
    store.mark_compiled("GameSource/B.cpp", "agent-a")
    store.review("GameSource/B.cpp", "reviewer", "pass", notes="gate-only")

    _goal, rows = store.next_tus(n=2)

    assert rows[0].id == "GameSource/A.cpp"
    assert rows[0].unresolved_deps == 0
    assert rows[1].id == "class:Utility"


def _write_workflow(tmp_path, status):
    progress = tmp_path / "progress"
    progress.mkdir()
    (progress / "tu_index.json").write_text(
        json.dumps(
            {
                "GameSource/A.cpp": {"source": "decfigs", "n_funcs": 1, "functions": ["A::Run"]},
                "GameSource/B.cpp": {"source": "decfigs", "n_funcs": 1, "functions": ["B::Run"]},
                "GameSource/C.cpp": {"source": "decfigs", "n_funcs": 1, "functions": ["C::Run"]},
                "GameSource/D.cpp": {"source": "decfigs", "n_funcs": 1, "functions": ["D::Run"]},
            }
        ),
        encoding="utf-8",
    )
    (progress / "status.json").write_text(json.dumps(status), encoding="utf-8")
    return tmp_path


def test_import_seeds_only_durable_statuses(tmp_path):
    """Snapshot import must apply only durable done/blocked states. Transient
    in_progress/compiled (and any owner) are server-born and would otherwise show up
    as stale, lease-less rows in Active Work -- the bug this guards against."""
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    workflow = _write_workflow(
        tmp_path,
        {
            "tu": {
                "GameSource/A.cpp": {"status": "done", "owner": "agent"},
                "GameSource/B.cpp": {"status": "blocked", "notes": "vendor code"},
                "GameSource/C.cpp": {"status": "in_progress", "owner": "agent"},
                "GameSource/D.cpp": {"status": "compiled", "owner": "agent"},
            }
        },
    )

    store.import_workflow(workflow, reset=True)

    _goal, counts, tus = store.snapshot()
    by_id = {tu.id: tu for tu in tus}
    # done/blocked applied, owner dropped; in_progress/compiled ignored -> stay todo.
    assert counts.done == 1
    assert counts.blocked == 1
    assert counts.in_progress == 0
    assert counts.compiled == 0
    assert counts.todo == 2
    assert by_id["GameSource/A.cpp"].owner is None
    assert by_id["GameSource/B.cpp"].notes == "vendor code"

    # Active Work on the dashboard is empty until a live claim is made on the server.
    assert store.dashboard_state()["active_work"] == []


def test_live_claim_survives_resync(tmp_path):
    """A re-sync (no reset) must not stomp a live server claim back to a snapshot state."""
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    workflow = _write_workflow(tmp_path, {"tu": {}})
    store.import_workflow(workflow, reset=True)

    store.claim("GameSource/C.cpp", "live-agent")
    # Workflow snapshot still thinks C is todo; a sync must leave the live claim intact.
    store.import_workflow(workflow, reset=False)

    _goal, _counts, tus = store.snapshot()
    by_id = {tu.id: tu for tu in tus}
    assert by_id["GameSource/C.cpp"].status == "in_progress"
    assert by_id["GameSource/C.cpp"].owner == "live-agent"


def test_expired_claim_returns_to_todo(tmp_path):
    store = make_store(tmp_path)
    store.claim("GameSource/B.cpp", "agent-a")

    with store.connect() as con:
        con.execute(
            "UPDATE tu SET lease_expires_at=? WHERE id='GameSource/B.cpp'",
            (iso(utcnow() - timedelta(minutes=1)),),
        )

    _goal, rows = store.next_tus(n=1)

    assert rows[0].id == "GameSource/B.cpp"
    _active_goal, counts, tus = store.snapshot()
    by_id = {tu.id: tu for tu in tus}
    assert counts.todo == 3
    assert by_id["GameSource/B.cpp"].owner is None
