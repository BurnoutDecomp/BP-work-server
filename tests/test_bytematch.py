from __future__ import annotations

import json

from fastapi.testclient import TestClient

from bp_work_server.api import create_app
from bp_work_server import history
from bp_work_server.store import WorkStore


def _summary(commit="aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111", exact_g=False, generated_at="2026-11-19T20:00:00Z"):
    """The shape tools/bytematch/ledger.py publishes as progress/bytematch.json."""
    g_status, g_score = ("exact", 100.0) if exact_g else ("nonmatching", 62.5)
    functions = [
        ["82100000", "BrnWorld::A::Run", 40, "exact", 100.0, 0, 0, 1, 0],
        ["82100040", "BrnWorld::A::Stop", 80, g_status, g_score, 0, 1, 1, 0],
        ["82100100", "BrnGui::B::Draw", 20, "missing", 0.0, -1, 0, 0, 1],
        ["82200000", "memcpy", 64, "sdk", 100.0, -1, 0, 2, 2],
        ["82300000", "sub_82300000", 12, "unnamed", 0.0, -1, 0, 3, -1],
    ]
    exact = 2 if exact_g else 1
    exact_bytes = 120 if exact_g else 40
    game = {"functions": 3, "bytes": 140, "exact": exact, "exact_bytes": exact_bytes,
            "nonmatching": 0 if exact_g else 1, "nonmatching_bytes": 0 if exact_g else 80,
            "missing": 1, "missing_bytes": 20, "fuzzy_bytes": float(exact_bytes + (0 if exact_g else 50))}
    first_exact = {"82100000": [commit, "2026-11-18T10:00:00Z"]}
    if exact_g:
        first_exact["82100040"] = [commit, generated_at]
    return {
        "meta": {"schema": 1, "branch": "x360", "b5_commit": commit, "generated_at": generated_at,
                 "verified_commit": commit, "behind": 0, "toolchain": "cl 14.00.6502 flags abc"},
        "totals": {"game": game, "sdk": {"functions": 1, "bytes": 64}, "unnamed": {"functions": 1, "bytes": 12},
                   "objects": {"sources": 1, "compiled": 1, "stale": 0, "unpaired": 2}},
        "areas": [{"area": "GameSource/World", "functions": 2, "bytes": 120, "exact": exact, "exact_bytes": exact_bytes,
                   "nonmatching": 0 if exact_g else 1, "nonmatching_bytes": 0, "missing": 0, "missing_bytes": 0,
                   "fuzzy_bytes": 90.0},
                  {"area": "GameSource/Gui", "functions": 1, "bytes": 20, "exact": 0, "exact_bytes": 0,
                   "nonmatching": 0, "nonmatching_bytes": 0, "missing": 1, "missing_bytes": 20, "fuzzy_bytes": 0.0}],
        "files": [{"source": "src/GameSource/World/A.cpp", "commit": commit, "measured": "2026-11-19T19:00:00Z",
                   "compiled": True, "stale": "current", "unpaired": 2, "functions": 2, "bytes": 120,
                   "exact": exact, "exact_bytes": exact_bytes, "nonmatching": 0 if exact_g else 1,
                   "nonmatching_bytes": 0, "missing": 0, "missing_bytes": 0, "fuzzy_bytes": 90.0}],
        "sources": ["src/GameSource/World/A.cpp"],
        "area_names": ["GameSource/Gui", "GameSource/World", "XDK/libcMT.lib", "unnamed"],
        "tu_names": ["GameSource/World/A.cpp", "class:BrnGui::B", "vendor:crt"],
        "columns": ["artist", "name", "size", "status", "score", "source", "stale", "area", "tu"],
        "functions": functions,
        "first_exact": first_exact,
        "recent": [{"artist": a, "name": "x", "size": 1, "source": None, "commit": v[0], "at": v[1]}
                   for a, v in sorted(first_exact.items(), key=lambda kv: kv[1][1], reverse=True)],
    }


def _workflow(tmp_path, name, summary):
    root = tmp_path / name
    progress = root / "progress"
    progress.mkdir(parents=True)
    (progress / "tu_index.json").write_text(json.dumps({
        "GameSource/World/A.cpp": {"source": "decfigs", "n_funcs": 2, "n_decfigs": 2,
                                   "functions": ["BrnWorld::A::Run", "BrnWorld::A::Stop"]},
    }), encoding="utf-8")
    if summary is not None:
        (progress / "bytematch.json").write_text(json.dumps(summary), encoding="utf-8")
    return root


def _store(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    return store


def test_import_summary_and_idempotency(tmp_path):
    store = _store(tmp_path)
    result = store.import_workflow(_workflow(tmp_path, "w1", _summary()))
    assert result["bytematch"] == 5
    # the same content under a new timestamp is not a new history point
    again = store.import_workflow(_workflow(tmp_path, "w2", _summary(generated_at="2026-11-19T21:00:00Z")))
    assert again["bytematch"] == 0
    s = store.bytematch_summary()
    assert s["available"] is True
    assert s["totals"]["game"]["exact"] == 1
    assert s["percent"]["exact_bytes"] == round(100 * 40 / 140, 2)
    assert s["status_counts"]["sdk"] == {"functions": 1, "bytes": 64}
    assert [a["area"] for a in s["areas"]] == ["GameSource/World", "GameSource/Gui"]
    assert len(s["history"]) == 1 and s["history"][0]["exact"] == 1
    with store.connect() as con:
        events = con.execute("SELECT detail_json FROM event WHERE action = 'bytematch'").fetchall()
    assert len(events) == 1 and json.loads(events[0]["detail_json"])["delta_exact"] == 1


def test_new_match_adds_a_history_point_and_an_event(tmp_path):
    store = _store(tmp_path)
    store.import_workflow(_workflow(tmp_path, "w1", _summary()))
    store.import_workflow(_workflow(tmp_path, "w2", _summary(commit="bbbb" * 10, exact_g=True)))
    s = store.bytematch_summary()
    assert [p["exact"] for p in s["history"]] == [1, 2]
    with store.connect() as con:
        last = con.execute("SELECT detail_json FROM event WHERE action = 'bytematch' ORDER BY id DESC").fetchone()
        points = history.points(con)
    assert json.loads(last["detail_json"])["delta_exact"] == 1
    assert points[-1]["bm_exact"] == 2 and points[-1]["bm_functions"] == 3


def test_absent_summary_keeps_the_panel_empty(tmp_path):
    store = _store(tmp_path)
    assert store.import_workflow(_workflow(tmp_path, "w1", None))["bytematch"] == 0
    assert store.bytematch_summary()["available"] is False
    assert store.bytematch_functions()["total"] == 0


def test_function_and_file_queries(tmp_path):
    store = _store(tmp_path)
    store.import_workflow(_workflow(tmp_path, "w1", _summary()))
    assert store.bytematch_functions(status="missing")["items"][0]["name"] == "BrnGui::B::Draw"
    found = store.bytematch_functions(q="0x82100040")["items"]
    assert [f["artist"] for f in found] == ["82100040"] and found[0]["stale"] == 1
    assert store.bytematch_functions(area="GameSource/World", sort="size", order="asc")["total"] == 2
    files = store.bytematch_files(q="World")
    assert files["total"] == 1 and files["items"][0]["fuzzy"] == round(100 * 90 / 120, 1)


def test_api_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_WORK_REQUIRE_TOKEN", "0")
    store = _store(tmp_path)
    store.import_workflow(_workflow(tmp_path, "w1", _summary()))
    client = TestClient(create_app(store))
    body = client.get("/api/bytematch/summary").json()
    assert body["available"] and body["meta"]["branch"] == "x360"
    page = client.get("/api/bytematch/functions", params={"status": "exact", "limit": 10}).json()
    assert page["total"] == 1 and page["items"][0]["first_exact"] == "2026-11-18T10:00:00Z"
    assert client.get("/api/bytematch/functions", params={"status": "bogus"}).status_code == 422
    assert client.get("/api/bytematch/files").json()["total"] == 1
    assert "bm_exact" in client.get("/api/history").json()["series"]


def test_map_snapshot_groups_like_the_progress_map(tmp_path):
    store = _store(tmp_path)
    store.import_workflow(_workflow(tmp_path, "w1", _summary()))
    snap = store.bytematch_map(include_functions=True)
    units = {u["id"]: u for u in snap["units"]}
    assert units["GameSource/World/A.cpp"]["status"] == "partial"
    assert units["GameSource/World/A.cpp"]["recorded_funcs"] == 1 and units["GameSource/World/A.cpp"]["source"] == "decfigs"
    assert units["class:BrnGui::B"]["status"] == "missing"
    assert units["vendor:crt"]["status"] == "sdk"
    bucket = units["unidentified:BURNOUT_X360_ARTIST.XEX"]
    assert bucket["unidentified"] and bucket["status"] == "unnamed"
    assert [f["artist"] for f in units["GameSource/World/A.cpp"]["functions"]] == ["82100000", "82100040"]
    assert snap["totals"] == {"tus": 3, "funcs": 5, "exact_funcs": 1, "matched_tus": 0, "bytes": 216, "exact_bytes": 40}
    assert store.bytematch_map()["units"][0]["functions"] is None
    store.import_workflow(_workflow(tmp_path, "w2", _summary(commit="bbbb" * 10, exact_g=True)))
    assert {u["id"]: u for u in store.bytematch_map()["units"]}["GameSource/World/A.cpp"]["status"] == "matched"
    assert store.bytematch_functions(tu="class:BrnGui::B")["items"][0]["name"] == "BrnGui::B::Draw"


def test_map_route_is_gzipped(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_WORK_REQUIRE_TOKEN", "0")
    store = _store(tmp_path)
    store.import_workflow(_workflow(tmp_path, "w1", _summary()))
    client = TestClient(create_app(store))
    response = client.get("/api/bytematch/map", params={"include_functions": "true"}, headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200 and len(response.json()["units"]) == 4


def test_migrate_adds_the_tu_column_to_an_older_store(tmp_path):
    import sqlite3
    from bp_work_server import bytematch
    con = sqlite3.connect(tmp_path / "old.sqlite3")
    con.execute("CREATE TABLE bytematch_func(artist TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, "
                "status TEXT NOT NULL, score REAL NOT NULL DEFAULT 0, source TEXT, stale INTEGER NOT NULL DEFAULT 0, "
                "area TEXT, first_exact TEXT)")
    bytematch.migrate(con)
    assert "tu" in {r[1] for r in con.execute("PRAGMA table_info(bytematch_func)")}
    bytematch.migrate(con)   # idempotent


def test_a_bad_summary_is_rejected_alone(tmp_path):
    store = _store(tmp_path)
    store.import_workflow(_workflow(tmp_path, "w1", _summary()))
    broken = _summary(commit="cccc" * 10)
    del broken["columns"]
    result = store.import_workflow(_workflow(tmp_path, "w2", broken))
    assert result["bytematch"] == -1 and result["tus"] == 1          # the ledger still imported
    assert store.bytematch_summary()["meta"]["b5_commit"] == "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM event WHERE action = 'bytematch_error'").fetchone()[0] == 1
