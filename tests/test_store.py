from __future__ import annotations

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
