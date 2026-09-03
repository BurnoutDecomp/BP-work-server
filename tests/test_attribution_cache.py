from __future__ import annotations

import json

from bp_work_server.attribution_cache import warm_attribution_cache
from bp_work_server.store import WorkStore, iso


def make_store(tmp_path) -> WorkStore:
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    with store.connect() as con:
        con.execute(
            """
            INSERT INTO tu(id, source, status, n_funcs, n_decfigs, dest_path, updated_at)
            VALUES
              ('GameSource/A.cpp', 'decfigs', 'done', 2, 2, 'b5-decomp/src/GameSource/A.cpp', ?),
              ('GameSource/B.cpp', 'decfigs', 'todo', 1, 1, 'b5-decomp/src/GameSource/B.cpp', ?),
              ('class:Utility', 'class', 'todo', 1, 0, NULL, ?)
            """,
            (iso(), iso(), iso()),
        )
        con.execute(
            "INSERT INTO func(name, tu_id, status) VALUES('A::Run', 'GameSource/A.cpp', 'reviewed')"
        )
        con.execute(
            "INSERT INTO func(name, tu_id, status) VALUES('A::Stop', 'GameSource/A.cpp', 'todo')"
        )
        con.execute(
            "INSERT INTO func(name, tu_id, status) VALUES('Utility::Fn', 'class:Utility', 'reviewed')"
        )
    return store


def test_warm_attribution_cache_populates_cacheable_reviewed_work(tmp_path):
    store = make_store(tmp_path)

    class FakeDecomp:
        def revision(self):
            return "rev-current"

        def history(self, dest_path):
            return [{"date": "2026-06-17T10:00:00+00:00", "name": "Niaz", "email": "n@example.test"}]

        def contributors(self, dest_path):
            return {
                "path": dest_path.removeprefix("b5-decomp/"),
                "basis": "surviving_lines",
                "contributors": [{"name": "Niaz", "email": "n@example.test", "lines": 4}],
            }

        def function_contributors(self, dest_path, function_name):
            return {
                "path": dest_path.removeprefix("b5-decomp/"),
                "basis": "surviving_lines",
                "line_range": [10, 20],
                "function_range_found": True,
                "contributors": [{"name": "Niaz", "email": "n@example.test", "lines": 3}],
            }

    result = warm_attribution_cache(store, FakeDecomp())

    assert result.files_cached == 1
    assert result.functions_cached == 2
    state = store.dashboard_state(attribution_repo_rev="rev-current")
    assert state["attribution_cache"]["file_cached"] == 1
    assert state["attribution_cache"]["file_total"] == 1
    assert state["attribution_cache"]["function_cached"] == 2
    assert state["attribution_cache"]["function_total"] == 2
    assert state["attribution_cache"]["file_complete"] is True
    assert state["attribution_cache"]["function_complete"] is True
    with store.connect() as con:
        rows = con.execute(
            "SELECT scope, dest_path, function_name, payload_json FROM attribution_cache ORDER BY scope"
        ).fetchall()
    assert [(row["scope"], row["function_name"]) for row in rows] == [
        ("file", ""),
        ("function", "A::Run"),
        ("function", "Utility::Fn"),
    ]
    assert json.loads(rows[0]["payload_json"])["contributors"]["contributors"][0]["name"] == "Niaz"


class RecordingDecomp:
    """A decomp stand-in that reports which revision it is at and what changed.

    Counts the blame-shaped calls so a test can assert what a warm recomputed
    rather than only what it stored.
    """

    def __init__(self, rev, changed=None, ancestor=True):
        self.rev = rev
        self.changed = changed if changed is not None else set()
        self.ancestor = ancestor
        self.file_calls = []
        self.function_calls = []

    def revision(self):
        return self.rev

    def candidate_paths(self, dest_path):
        rel = dest_path.removeprefix("b5-decomp/")
        return [rel, rel[:-2] + ".cpp"] if rel.endswith(".h") else [rel]

    def changed_paths(self, base_rev, head_rev):
        if not self.ancestor:
            return None
        return set(self.changed)

    def history(self, dest_path):
        self.file_calls.append(dest_path)
        return [{"date": "2026-06-17T10:00:00+00:00", "name": self.rev, "email": "n@example.test"}]

    def contributors(self, dest_path):
        return {
            "path": dest_path.removeprefix("b5-decomp/"),
            "basis": "surviving_lines",
            "contributors": [{"name": self.rev, "email": "n@example.test", "lines": 4}],
        }

    def function_contributors(self, dest_path, function_name):
        self.function_calls.append((dest_path, function_name))
        return {
            "path": dest_path.removeprefix("b5-decomp/"),
            "basis": "surviving_lines",
            "contributors": [{"name": self.rev, "email": "n@example.test", "lines": 3}],
        }


def test_warm_carries_untouched_files_forward(tmp_path):
    """A push that touches nothing cacheable must not re-blame the whole tree."""
    store = make_store(tmp_path)
    warm_attribution_cache(store, RecordingDecomp("rev-1"))

    decomp = RecordingDecomp("rev-2", changed={"src/GameSource/Untracked.cpp"})
    result = warm_attribution_cache(store, decomp)

    assert result.base_rev == "rev-1"
    assert (result.files_reused, result.functions_reused) == (1, 2)
    assert decomp.file_calls == []
    assert decomp.function_calls == []
    # Carried-forward payloads are re-stamped under the new revision, so the
    # dashboard reads them as a complete pass at the current tip.
    state = store.dashboard_state(attribution_repo_rev="rev-2")
    assert state["attribution_cache"]["file_complete"] is True
    assert state["attribution_cache"]["function_complete"] is True


def test_warm_recomputes_only_the_files_a_push_touched(tmp_path):
    store = make_store(tmp_path)
    warm_attribution_cache(store, RecordingDecomp("rev-1"))

    # A.cpp changed; the class TU's home file did not.
    decomp = RecordingDecomp("rev-2", changed={"src/GameSource/A.cpp"})
    result = warm_attribution_cache(store, decomp)

    assert decomp.file_calls == ["b5-decomp/src/GameSource/A.cpp"]
    assert decomp.function_calls == [("b5-decomp/src/GameSource/A.cpp", "A::Run")]
    assert (result.files_reused, result.functions_reused) == (0, 1)
    with store.connect() as con:
        payload = con.execute(
            "SELECT payload_json FROM attribution_cache "
            "WHERE scope='file' AND dest_path='b5-decomp/src/GameSource/A.cpp'"
        ).fetchone()["payload_json"]
    assert json.loads(payload)["contributors"]["contributors"][0]["name"] == "rev-2"


def test_warm_recomputes_a_header_whose_cpp_sibling_changed(tmp_path):
    """A *.h blames through its .cpp, so an edit there invalidates the header."""
    store = make_store(tmp_path)
    with store.connect() as con:
        con.execute(
            "UPDATE tu SET dest_path='b5-decomp/src/GameSource/A.h' WHERE id='GameSource/A.cpp'"
        )
    warm_attribution_cache(store, RecordingDecomp("rev-1"))

    decomp = RecordingDecomp("rev-2", changed={"src/GameSource/A.cpp"})
    result = warm_attribution_cache(store, decomp)

    assert decomp.file_calls == ["b5-decomp/src/GameSource/A.h"]
    assert result.files_reused == 0


def test_warm_recomputes_everything_when_the_base_is_not_an_ancestor(tmp_path):
    """A force-push invalidates every cached blame; nothing may be carried over."""
    store = make_store(tmp_path)
    warm_attribution_cache(store, RecordingDecomp("rev-1"))

    decomp = RecordingDecomp("rev-2", ancestor=False)
    result = warm_attribution_cache(store, decomp)

    assert result.base_rev is None
    assert (result.files_reused, result.functions_reused) == (0, 0)
    assert decomp.file_calls == ["b5-decomp/src/GameSource/A.cpp"]


def test_warm_computes_targets_the_previous_pass_never_covered(tmp_path):
    """Newly finished work is computed even when its file did not change."""
    store = make_store(tmp_path)
    warm_attribution_cache(store, RecordingDecomp("rev-1"))
    with store.connect() as con:
        con.execute("UPDATE func SET status='reviewed' WHERE name='A::Stop'")

    decomp = RecordingDecomp("rev-2")
    result = warm_attribution_cache(store, decomp)

    assert decomp.function_calls == [("b5-decomp/src/GameSource/A.cpp", "A::Stop")]
    assert result.functions_reused == 2


def test_full_warm_ignores_the_cache(tmp_path):
    store = make_store(tmp_path)
    warm_attribution_cache(store, RecordingDecomp("rev-1"))

    decomp = RecordingDecomp("rev-2")
    result = warm_attribution_cache(store, decomp, full=True)

    assert result.base_rev is None
    assert result.files_reused == 0
    assert decomp.file_calls == ["b5-decomp/src/GameSource/A.cpp"]
