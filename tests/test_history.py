"""The evolution layer: ring snapshots per import, the merged time series, the backfill file."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from bp_work_server import history
from bp_work_server.api import create_app
from bp_work_server.store import WorkStore


def _workflow(tmp_path, done=("GameSource/A.cpp",)):
    progress = tmp_path / "progress"
    progress.mkdir(parents=True, exist_ok=True)
    (progress / "tu_index.json").write_text(
        json.dumps(
            {
                "GameSource/A.cpp": {"source": "decfigs", "n_funcs": 2, "functions": ["A::Run", "A::Stop"]},
                "GameSource/B.cpp": {"source": "decfigs", "n_funcs": 1, "functions": ["B::Run"]},
            }
        ),
        encoding="utf-8",
    )
    status = {"tu": {tu: {"status": "done"} for tu in done}, "func": {}}
    if "GameSource/A.cpp" in done:
        status["func"] = {"A::Run": {"status": "reviewed"}, "A::Stop": {"status": "reviewed"}}
    (progress / "status.json").write_text(json.dumps(status), encoding="utf-8")
    return tmp_path


def test_import_records_one_snapshot_per_change(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    wf = _workflow(tmp_path / "wf")
    store.import_workflow(wf)
    store.import_workflow(wf)  # nothing moved: no second point
    pts = store.history_points()["points"]
    assert len(pts) == 1
    p = pts[0]
    assert p["tu_total"] == 2 and p["tu_done"] == 1 and p["tu_todo"] == 1
    assert p["funcs_total"] == 3 and p["funcs_done"] == 2 and p["funcs_named_uncovered"] == 1
    assert p["sources"] == ["snapshot"]
    # a real change adds a point
    _workflow(tmp_path / "wf", done=("GameSource/A.cpp", "GameSource/B.cpp"))
    store.import_workflow(wf)
    pts = store.history_points()["points"]
    assert len(pts) == 2 and pts[-1]["tu_done"] == 2


def test_points_merge_audit_runs_and_carry_values_forward(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    with store.connect(ensure_wal=True) as con:
        con.executescript(history.SCHEMA)
        history.record(con, "2026-06-11T10:00:00+02:00", "aaa", values={"tu_total": 10, "tu_done": 1})
        con.execute(
            "INSERT INTO audit_run(kind, commit_hash, imported_at, stats_json) VALUES('funcaudit', 'b1', "
            "'2026-06-12T09:00:00+00:00', ?)",
            (json.dumps({"paired": 5, "clean": 2, "no_body": 7, "weight": 9}),),
        )
        history.record(con, "2026-06-13T10:00:00+00:00", "bbb", values={"tu_total": 10, "tu_done": 3})
        pts = history.points(con)
        recent = history.points(con, days=1)
    assert [p["date"] for p in pts] == ["2026-06-11", "2026-06-12", "2026-06-13"]
    assert pts[0]["ts"] == "2026-06-11T08:00:00+00:00"          # rewritten in UTC
    assert "paired" not in pts[0]
    assert pts[1]["paired"] == 5 and pts[1]["tu_done"] == 1     # snapshot carried forward
    assert pts[2]["tu_done"] == 3 and pts[2]["clean"] == 2      # audit carried forward
    assert recent == []                                          # the window is by date


def test_one_point_per_day_even_when_nothing_moved(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    with store.connect(ensure_wal=True) as con:
        con.executescript(history.SCHEMA)
        same = {"tu_total": 10, "tu_done": 1}
        assert history.record(con, "2026-06-11T10:00:00+00:00", "a", values=same)
        assert not history.record(con, "2026-06-11T18:00:00+00:00", "b", values=same)   # same day, same totals
        assert history.record(con, "2026-06-12T00:10:00+00:00", "c", values=same)       # a new day
        assert history.record(con, "2026-06-12T00:11:00+00:00", "d", values={"tu_total": 10, "tu_done": 2})
        assert history.has_point_today(con, "2026-06-12T23:00:00+00:00")
        assert not history.has_point_today(con, "2026-06-13T00:00:00+00:00")
    # the daily tick: the first call of a day records, the second does not
    assert store.record_daily_snapshot()
    assert not store.record_daily_snapshot()
    pts = store.history_points()["points"]
    assert pts[-1]["sources"] == ["snapshot"] and pts[-1]["date"] == pts[-1]["ts"][:10]


def test_seconds_until_daily_tick():
    from datetime import datetime, timezone

    at = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    assert history.seconds_until_daily_tick(at) == 600
    late = datetime(2026, 9, 20, 0, 10, tzinfo=timezone.utc)
    assert history.seconds_until_daily_tick(late) == 86400


def test_backfill_file_import_skips_known_commits(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    f = tmp_path / "snaps.json"
    f.write_text(json.dumps({"snapshots": [
        {"ts": "2026-06-11T10:00:00+00:00", "commit": "c1", "metrics": {"tu_total": 4, "tu_done": 1, "tu_linked": None}},
        {"ts": "2026-06-12T10:00:00+00:00", "commit": "c2", "metrics": {"tu_total": 4, "tu_done": 2, "tu_linked": 1}},
    ]}), encoding="utf-8")
    assert store.import_history_file(f) == 2
    assert store.import_history_file(f) == 0
    pts = store.history_points()["points"]
    assert pts[0]["tu_linked"] is None and pts[1]["tu_linked"] == 1


def test_history_endpoint(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    store.import_workflow(_workflow(tmp_path / "wf"))
    client = TestClient(create_app(store))
    body = client.get("/api/history").json()
    assert body["points"][0]["tu_done"] == 1
    assert body["series"]["clean"] == "paired"
    assert client.get("/api/history?days=0").status_code == 422
