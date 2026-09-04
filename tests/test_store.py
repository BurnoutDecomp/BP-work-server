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


def test_dashboard_returns_all_blocked_tus(tmp_path):
    store = make_store(tmp_path)
    blocked_ids = [f"GameSource/Blocked{i:02}.cpp" for i in range(60)]
    with store.connect() as con:
        con.executemany(
            """
            INSERT INTO tu(id, source, status, n_funcs, n_decfigs, updated_at)
            VALUES(?, 'decfigs', 'blocked', 0, 0, ?)
            """,
            [(tu_id, iso()) for tu_id in blocked_ids],
        )

    state = store.dashboard_state()

    assert state["counts"]["blocked"] == 60
    assert len(state["blocked"]) == 60
    assert {item["id"] for item in state["blocked"]} == set(blocked_ids)


def test_actor_maps_canonicalize_github_and_case_aliases(tmp_path):
    store = make_store(tmp_path)
    store.create_worker("Adriwin", github_username="adriwin06")
    store.create_worker("Derneuere")
    with store.users_connect() as con:
        con.execute(
            "INSERT INTO worker_alias(alias, username, kind) VALUES(?, ?, ?)",
            ("Nathan V.", "Derneuere", "git-name"),
        )
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
    assert store.canonical_actor("Nathan V.") == "Derneuere"
    assert {event["agent"] for event in state["recent_events"]} == {"Adriwin", "Derneuere"}


def test_actor_maps_include_known_git_identity_defaults(tmp_path):
    store = make_store(tmp_path)
    store.create_worker("Derneuere")
    store.create_worker("JeBobs")

    assert store.canonical_actor("Niaz") == "Derneuere"
    assert store.canonical_actor("tigrexspalterlp@gmail.com") == "Derneuere"
    assert store.canonical_actor("Nathan V.") == "JeBobs"

    with store.users_connect() as con:
        con.execute(
            "INSERT INTO worker_alias(alias, username, kind) VALUES(?, ?, ?)",
            ("Nathan V.", "Derneuere", "manual"),
        )

    assert store.canonical_actor("Nathan V.") == "Derneuere"


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


def test_reset_tu_returns_functions_to_todo_and_clears_completion(tmp_path):
    store = make_store(tmp_path)
    store.claim("GameSource/B.cpp", "agent-a")
    store.mark_compiled("GameSource/B.cpp", "agent-a")
    store.review("GameSource/B.cpp", "agent-a", "pass", notes="gate-only")

    store.reset_tu("GameSource/B.cpp", "agent-a", notes="returned to queue")
    detail = store.tu_detail("GameSource/B.cpp")

    assert detail["status"] == "todo"
    assert detail["completed_by"] is None
    assert detail["funcs"][0]["status"] == "todo"
    assert detail["funcs"][0]["completed_by"] is None


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


def test_import_derives_destinations_for_class_tus(tmp_path):
    progress = tmp_path / "progress"
    progress.mkdir()
    (progress / "tu_index.json").write_text(
        json.dumps(
            {
                "class:BrnGameState::ScoringSystem": {
                    "source": "class",
                    "n_funcs": 1,
                    "functions": ["BrnGameState::ScoringSystem::AddPlayer"],
                },
                "class:<global>": {
                    "source": "class",
                    "n_funcs": 1,
                    "functions": ["GlobalFn"],
                },
            }
        ),
        encoding="utf-8",
    )
    (progress / "status.json").write_text(json.dumps({"tu": {}}), encoding="utf-8")
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()

    store.import_workflow(tmp_path, reset=True)

    with store.connect() as con:
        rows = {
            row["id"]: row["dest_path"]
            for row in con.execute("SELECT id, dest_path FROM tu ORDER BY id")
        }
    assert (
        rows["class:BrnGameState::ScoringSystem"]
        == "b5-decomp/src/classes/BrnGameState/ScoringSystem.cpp"
    )
    assert rows["class:<global>"] == "b5-decomp/src/classes/global.cpp"


def test_migrate_backfills_missing_class_destinations(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    with store.connect() as con:
        con.execute(
            """
            INSERT INTO tu(id, source, status, n_funcs, n_decfigs, dest_path, updated_at)
            VALUES('class:Utility::Parser', 'class', 'todo', 1, 0, NULL, ?)
            """,
            (iso(),),
        )

    store.migrate()

    with store.connect() as con:
        row = con.execute(
            "SELECT dest_path FROM tu WHERE id='class:Utility::Parser'"
        ).fetchone()
    assert row["dest_path"] == "b5-decomp/src/classes/Utility/Parser.cpp"


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


def test_resync_prunes_tus_and_functions_removed_from_authoritative_index(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    workflow = _write_workflow(tmp_path, {"tu": {}})
    store.import_workflow(workflow, reset=True)
    with store.connect() as con:
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "GameSource/D.cpp", "agent", "review_pass", "{}"),
        )

    (workflow / "progress" / "tu_index.json").write_text(
        json.dumps(
            {
                "GameSource/A.cpp": {
                    "source": "decfigs",
                    "n_funcs": 1,
                    "functions": ["A::Renamed"],
                }
            }
        ),
        encoding="utf-8",
    )
    store.import_workflow(workflow, reset=False)

    with store.connect() as con:
        assert [row["id"] for row in con.execute("SELECT id FROM tu")] == ["GameSource/A.cpp"]
        assert [row["name"] for row in con.execute("SELECT name FROM func")] == ["A::Renamed"]
        assert con.execute(
            "SELECT COUNT(*) FROM event WHERE tu_id='GameSource/D.cpp' AND action='review_pass'"
        ).fetchone()[0] == 1


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


def test_covered_funcs_count_reviewed_functions_outside_done_tus(tmp_path):
    """A reviewed function counts even when its TU is still blocked.

    Counting whole done TUs instead dropped 1,401 reviewed functions on
    production and understated the Functions ring by five points.
    """
    store = make_store(tmp_path)
    with store.connect() as con:
        # A.cpp is finished; B.cpp is blocked but one of its functions is reviewed.
        con.execute("UPDATE tu SET status='done' WHERE id='GameSource/A.cpp'")
        con.execute("UPDATE func SET status='reviewed' WHERE tu_id='GameSource/A.cpp'")
        con.execute("UPDATE tu SET status='blocked' WHERE id='GameSource/B.cpp'")
        con.execute("UPDATE func SET status='reviewed' WHERE name='B::Run'")

    totals = store.dashboard_state()["totals"]

    assert totals["done_tus"] == 1
    assert totals["done_funcs"] == 3


def test_contribution_counts_hold_the_last_complete_revision_while_warming(tmp_path):
    """A new decomp commit must not blank every contributor to zero.

    A warm writes its whole revision in one transaction at the end, so the new
    revision has no rows until it finishes; reading it anyway showed an empty
    roster for the length of the warm.
    """
    store = make_store(tmp_path)
    store.create_worker("Adriwin", github_username="Adriwin06")
    payload = json.dumps(
        {
            "latest": None,
            "contributors": {
                "basis": "surviving_lines",
                "contributors": [
                    {
                        "name": "Adriwin",
                        "email": "1+Adriwin06@users.noreply.github.com",
                        "lines": 40,
                    }
                ],
            },
        }
    )
    with store.connect() as con:
        con.execute("UPDATE tu SET status='done' WHERE id='GameSource/A.cpp'")
        con.execute(
            """
            INSERT INTO attribution_cache(
                scope, dest_path, function_name, repo_rev, payload_json, updated_at
            )
            VALUES('file', 'b5-decomp/src/GameSource/A.cpp', '', 'oldrev', ?, ?)
            """,
            (payload, iso()),
        )

    warmed = store.dashboard_state(attribution_repo_rev="oldrev")
    by_name = {agent["name"]: agent for agent in warmed["agents"]}
    assert by_name["Adriwin"]["contributed_tus"] == 1

    # A newer tip with nothing cached yet keeps serving the previous pass, and
    # says which revision the numbers actually came from.
    warming = store.dashboard_state(attribution_repo_rev="newrev")
    by_name = {agent["name"]: agent for agent in warming["agents"]}
    assert by_name["Adriwin"]["contributed_tus"] == 1
    assert warming["attribution_cache"]["repo_rev"] == "newrev"
    assert warming["attribution_cache"]["counts_repo_rev"] == "oldrev"


def _workflow_with_unidentified(tmp_path, functions=None):
    """A minimal workflow checkout carrying an unidentified-function table."""
    progress = tmp_path / "progress"
    progress.mkdir(parents=True, exist_ok=True)
    (progress / "tu_index.json").write_text(
        json.dumps(
            {
                "GameSource/A.cpp": {
                    "source": "decfigs",
                    "n_funcs": 2,
                    "n_decfigs": 2,
                    "functions": ["A::Run", "A::Stop"],
                }
            }
        ),
        encoding="utf-8",
    )
    (progress / "status.json").write_text(
        json.dumps({"tu": {"GameSource/A.cpp": {"status": "done"}},
                    "func": {"A::Run": {"status": "reviewed"},
                             "A::Stop": {"status": "reviewed"}}}),
        encoding="utf-8",
    )
    if functions is None:
        functions = [
            {"addr": "0x82000000", "name": "sub_82000000", "insns": 40},
            {"addr": "0x82000100", "name": "sub_82000100", "insns": 12},
            {"addr": "0x82000200", "name": "sub_82000200", "insns": 7},
        ]
    (progress / "unidentified.json").write_text(
        json.dumps({"binary": "TEST.XEX", "exported": 5, "identified": 2,
                    "thunks_skipped": 1, "instructions": 59, "functions": functions}),
        encoding="utf-8",
    )
    return tmp_path


def test_unidentified_functions_join_the_function_totals(tmp_path):
    """The denominator is the binary, not the part of it that has names.

    Leaving unnamed functions out measured completion against work already
    identified: on production 2,533 functions -- ~6% of the executable -- were
    neither done nor todo, just absent.
    """
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    result = store.import_workflow(_workflow_with_unidentified(tmp_path / "wf"), reset=True)
    assert result["unidentified"] == 3

    totals = store.dashboard_state()["totals"]
    assert totals["identified_funcs"] == 2
    assert totals["unidentified_funcs"] == 3
    assert totals["funcs"] == 5
    assert totals["done_funcs"] == 2
    assert totals["func_percent"] == 40.0


def test_unidentified_bucket_is_not_a_translation_unit(tmp_path):
    """It exists only because func.tu_id is NOT NULL; counting it would be a
    second lie in the opposite direction."""
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    store.import_workflow(_workflow_with_unidentified(tmp_path / "wf"), reset=True)

    state = store.dashboard_state()
    assert state["totals"]["tus"] == 1
    assert state["counts"]["todo"] == 0
    assert state["counts"]["done"] == 1
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM tu").fetchone()[0] == 2  # bucket is present


def test_unidentified_bucket_is_never_queued_or_claimable(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    store.import_workflow(_workflow_with_unidentified(tmp_path / "wf"), reset=True)

    _goal, items = store.next_tus(n=50)
    assert all(not item.id.startswith("unidentified:") for item in items)
    with pytest.raises(ValueError, match="not claimable"):
        store.claim("unidentified:TEST.XEX", "someone")


def test_unidentified_bucket_keeps_a_null_destination(tmp_path):
    """A synthesised path would send Git attribution hunting a file that cannot exist."""
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    store.import_workflow(_workflow_with_unidentified(tmp_path / "wf"), reset=True)
    store.migrate()  # backfill pass runs here

    with store.connect() as con:
        dest = con.execute(
            "SELECT dest_path FROM tu WHERE id='unidentified:TEST.XEX'"
        ).fetchone()["dest_path"]
    assert dest is None


def test_naming_a_function_removes_it_from_the_unidentified_bucket(tmp_path):
    """The count has to fall as work happens, or it is just another frozen number."""
    workflow = _workflow_with_unidentified(tmp_path / "wf")
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    store.import_workflow(workflow, reset=True)
    assert store.dashboard_state()["totals"]["unidentified_funcs"] == 3

    # sub_82000100 gets identified: it leaves unidentified.json on the next build.
    (workflow / "progress" / "unidentified.json").write_text(
        json.dumps({"binary": "TEST.XEX", "functions": [
            {"addr": "0x82000000", "name": "sub_82000000", "insns": 40},
            {"addr": "0x82000200", "name": "sub_82000200", "insns": 7},
        ]}),
        encoding="utf-8",
    )
    store.import_workflow(workflow)

    totals = store.dashboard_state()["totals"]
    assert totals["unidentified_funcs"] == 2
    assert totals["funcs"] == 4
    with store.connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM func WHERE name='sub_82000100'"
        ).fetchone()[0] == 0


def test_workflow_without_the_table_has_no_bucket(tmp_path):
    """An older checkout, or one built on a machine with no IDA export."""
    workflow = _workflow_with_unidentified(tmp_path / "wf")
    (workflow / "progress" / "unidentified.json").unlink()
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    result = store.import_workflow(workflow, reset=True)

    assert result["unidentified"] == 0
    totals = store.dashboard_state()["totals"]
    assert totals["funcs"] == 2
    assert totals["unidentified_funcs"] == 0
