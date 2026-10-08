from __future__ import annotations

from fastapi.testclient import TestClient

from bp_work_server.api import create_app
from bp_work_server.store import WorkStore, iso


def make_map_client(tmp_path):
    store = WorkStore(tmp_path / "map.sqlite3")
    store.migrate()
    with store.connect() as con:
        for tu, source, status, linked in [
            ("GameSource/Open.cpp", "decfigs", "blocked", 1),
            ("class:Done", "class", "done", 0),
            ("GameSource/Empty.h", "decfigs", "todo", 0),
            ("unidentified:console", "unidentified", "todo", 0),
        ]:
            con.execute(
                "INSERT INTO tu(id, source, status, linked, n_funcs, updated_at) "
                "VALUES(?, ?, ?, ?, 99, ?)", (tu, source, status, linked, iso()),
            )
        for name, tu, status in [
            ("Open::Reviewed", "GameSource/Open.cpp", "reviewed"),
            ("Open::Pending", "GameSource/Open.cpp", "todo"),
            ("Done::StillPending", "class:Done", "todo"),
            ("Done::Recovered", "class:Done", "recovered"),
            ("Done::Compiled", "class:Done", "compiles"),
            ("sub_100", "unidentified:console", "todo"),
        ]:
            con.execute("INSERT INTO func(name, tu_id, status) VALUES(?, ?, ?)", (name, tu, status))
        con.execute("INSERT INTO goal(name) VALUES('boot')")
        con.execute("INSERT INTO goal_tu(goal_name, tu_id) VALUES('boot', 'GameSource/Open.cpp')")
    return TestClient(create_app(store)), store


def test_snapshot_distinguishes_unit_status_from_individual_function_status(tmp_path):
    client, store = make_map_client(tmp_path)
    compact = client.get("/api/progress-map").json()
    full = client.get("/api/progress-map?include_functions=true").json()
    assert compact["include_functions"] is False
    assert all("functions" not in unit for unit in compact["units"])
    units = {unit["id"]: unit for unit in full["units"]}
    opened = units["GameSource/Open.cpp"]
    assert opened["status"] == "blocked"
    assert opened["function_count"] == 2  # Actual rows, not the declared 99.
    assert opened["recorded_funcs"] == 1
    assert opened["goals"] == ["boot"]
    assert opened["linked"] is True
    assert {fn["name"]: fn["status"] for fn in opened["functions"]} == {
        "Open::Pending": "todo", "Open::Reviewed": "reviewed",
    }
    assert units["class:Done"]["status"] == "done"
    assert units["class:Done"]["recorded_funcs"] == 2  # Done TU does not make all functions done.
    assert units["GameSource/Empty.h"]["function_count"] == 0
    assert units["GameSource/Empty.h"]["functions"] == []
    assert units["unidentified:console"]["unidentified"] is True
    # Both TU views exclude the synthetic function bucket; the function list
    # still exposes every real function, including unidentified ones.
    tu_list = client.get("/api/tus").json()
    assert tu_list["total"] == 3
    assert all(item["source"] != "unidentified" for item in tu_list["items"])
    assert client.get("/api/funcs").json()["total"] == 6
    assert compact["totals"] == full["totals"] == {
        "tus": 3, "funcs": 6, "done_tus": 1, "done_funcs": 3,
        "linked_tus": 1, "unidentified_funcs": 1,
    }
    dashboard = store.dashboard_state()["totals"]
    for key, value in full["totals"].items():
        assert dashboard[key] == value


def test_map_cache_is_bounded_and_invalidated_with_work_changes(tmp_path, monkeypatch):
    from bp_work_server.services.dashboard import invalidate_dashboard_cache
    from starlette.requests import Request

    client, store = make_map_client(tmp_path)
    original = store.progress_map
    calls = []

    def counted(*, include_functions=False):
        calls.append(include_functions)
        return original(include_functions=include_functions)

    monkeypatch.setattr(store, "progress_map", counted)
    client.get("/api/progress-map")
    client.get("/api/progress-map")
    client.get("/api/progress-map?include_functions=true")
    client.get("/api/progress-map?include_functions=true")
    assert calls == [False, True]
    with store.connect() as con:
        con.execute("UPDATE func SET status='reviewed' WHERE name='Open::Pending'")
    invalidate_dashboard_cache(Request({"type": "http", "app": client.app}))
    refreshed = client.get("/api/progress-map?include_functions=true").json()
    assert calls == [False, True, True]
    assert refreshed["totals"]["done_funcs"] == 4
    client.app.state.progress_map_cache[True]["expires_at"] = 0
    client.get("/api/progress-map?include_functions=true")
    assert calls == [False, True, True, True]


def test_map_compression_and_empty_snapshot(tmp_path):
    client, store = make_map_client(tmp_path)
    compressed = client.get("/api/progress-map?include_functions=true",
                            headers={"Accept-Encoding": "gzip"})
    plain = client.get("/api/progress-map?include_functions=true",
                       headers={"Accept-Encoding": "identity"})
    assert compressed.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in plain.headers
    assert compressed.json() == plain.json()
    with store.connect() as con:
        con.execute("DELETE FROM goal_tu")
        con.execute("DELETE FROM tu")
    assert store.progress_map(include_functions=True)["units"] == []
    assert not any(store.progress_map()["totals"].values())
