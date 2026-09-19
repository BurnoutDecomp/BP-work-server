from __future__ import annotations

import json

from fastapi.testclient import TestClient

from bp_work_server.api import create_app
from bp_work_server.audit import audit_file_for_dest, finding_weight
from bp_work_server.store import WorkStore


def _workflow(tmp_path, commit="aaaa1111", author="Adriwin", weight_variant=0):
    """A minimal BP-Decomp_Workflow checkout carrying both audit reports."""
    root = tmp_path / f"workflow-{commit}"
    progress = root / "progress"
    progress.mkdir(parents=True)
    (progress / "tu_index.json").write_text(
        json.dumps(
            {
                "GameSource/World/A.cpp": {
                    "source": "decfigs",
                    "n_funcs": 2,
                    "n_decfigs": 2,
                    "functions": ["BrnWorld::A::Run", "BrnWorld::A::Stop"],
                },
                "GameSource/World/B.cpp": {
                    "source": "decfigs",
                    "n_funcs": 1,
                    "n_decfigs": 1,
                    "functions": ["BrnWorld::B::Run"],
                },
            }
        ),
        encoding="utf-8",
    )
    run_findings = {
        "MISSING_CASE": ["3, 4"],
        "MISSING_CALLEE": ["BrnWorld::RemoveRivals"],
        "MISSING_STRING": ['"a log line"'],
        "UNCITED_DATA": ["0x82FAD400", "0x82FAD4E0"],
    }
    if weight_variant:
        run_findings = {"MISSING_STRING": ['"a log line"']}  # the high-signal items got fixed
    (progress / "funcaudit.json").write_text(
        json.dumps(
            {
                "meta": {"tool": "funcaudit", "generated_at": "2026-09-19T17:00:00Z",
                         "b5_commit": commit, "b5_author": author},
                "stats": {"paired": 3, "clean": 1 + weight_variant, "no_body": 1,
                          "unpaired_no_file": 0, "no_export": 0},
                "categories": {"MISSING_CASE": {"items": 1, "functions": 1}},
                "results": [
                    {"tu": "GameSource/World/A.cpp", "name": "BrnWorld::A::Run",
                     "addr": "0x82000010", "file": "GameSource/World/A.cpp", "line": 10,
                     "flagged": True, "helpers": 1, "findings": run_findings},
                    {"tu": "GameSource/World/B.cpp", "name": "BrnWorld::B::Run",
                     "addr": "0x82000020", "file": "GameSource/World/B.cpp", "line": 0,
                     "flagged": False, "helpers": 0,
                     "findings": {"NO_BODY": ["identity names GameSource/World/B.cpp; no definition anywhere"]}},
                ],
            }
        ),
        encoding="utf-8",
    )
    (progress / "stubs.json").write_text(
        json.dumps(
            {
                "meta": {"tool": "stubaudit", "generated_at": "2026-09-19T17:00:30Z",
                         "b5_commit": commit, "b5_author": author},
                "stats": {"stubs": 2, "high": 1, "medium": 0, "low": 1, "files": 1,
                          "with_console": 2, "live": 1, "console_lines": 300},
                "rows": [
                    {"file": "GameSource/World/A.cpp", "line": 40, "name": "BrnWorld::A::Stop",
                     "addr": "0x82000030", "tier": "HIGH", "why": "trap body",
                     "console_lines": 250, "callers": 2,
                     "live_callers": ["BrnWorld::A::Run"], "top": "GameSource/World"},
                    {"file": "GameSource/World/A.cpp", "line": 60, "name": "BrnWorld::A::Tick",
                     "addr": "0x82000040", "tier": "LOW", "why": "trivial body, unmarked",
                     "console_lines": 50, "callers": 0, "live_callers": [],
                     "top": "GameSource/World"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


def test_weight_counts_only_high_signal_categories():
    assert finding_weight({"MISSING_CASE": ["1"], "MISSING_STRING": ["x", "y"], "NO_BODY": ["z"]}) == 2
    assert audit_file_for_dest("b5-decomp/src/GameSource/World/A.cpp") == "GameSource/World/A.cpp"
    assert audit_file_for_dest(None) is None


def test_import_loads_findings_rollups_and_summary(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    result = store.import_workflow(_workflow(tmp_path))

    assert result["audit_findings"] == 2
    assert result["stubs"] == 2

    summary = store.audit_summary()
    assert summary["funcaudit"]["paired"] == 3
    assert summary["funcaudit"]["clean"] == 1
    assert summary["funcaudit"]["weight"] == 3  # MISSING_CASE 1 + MISSING_CALLEE 1 + NO_BODY 1
    assert summary["funcaudit"]["verified_percent"] == 33.3
    assert summary["funcaudit"]["commit"] == "aaaa1111"
    assert summary["stubs"]["stubs"] == 2
    assert summary["stubs"]["live"] == 1
    assert len(summary["history"]) == 1
    assert summary["history"][0]["verified_percent"] == 33.3
    assert summary["history"][0]["stubs_live"] == 1

    files = store.audit_files()
    assert files["total"] == 2
    top = files["items"][0]
    assert top["file"] == "GameSource/World/A.cpp"
    assert top["weight"] == 2
    assert top["missing_case"] == 1
    assert top["uncited_data"] == 2

    only_no_body = store.audit_files(category="NO_BODY")
    assert [item["file"] for item in only_no_body["items"]] == ["GameSource/World/B.cpp"]

    functions = store.audit_functions("GameSource/World/A.cpp")
    assert functions["tu_id"] == "GameSource/World/A.cpp"
    assert functions["items"][0]["findings"]["MISSING_CALLEE"] == ["BrnWorld::RemoveRivals"]
    assert functions["items"][0]["flagged"] is True

    stub_files = store.stub_files(live_only=True)
    assert stub_files["total"] == 1
    assert stub_files["items"][0]["high"] == 1
    stubs = store.stubs("GameSource/World/A.cpp")
    assert [s["tier"] for s in stubs["items"]] == ["HIGH", "LOW"]
    assert stubs["items"][0]["live_callers"] == ["BrnWorld::A::Run"]


def test_import_is_idempotent_per_commit_and_logs_a_delta_for_a_new_one(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    first = _workflow(tmp_path)
    store.import_workflow(first)
    store.import_workflow(first)  # a re-sync of the same snapshot
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM audit_run").fetchone()[0] == 2  # one per kind
        assert con.execute("SELECT COUNT(*) FROM event WHERE action IN ('audit','stubs')").fetchone()[0] == 0

    store.import_workflow(_workflow(tmp_path, commit="bbbb2222", author="JeBobs", weight_variant=1))
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM audit_run WHERE kind='funcaudit'").fetchone()[0] == 2
        row = con.execute("SELECT agent, detail_json FROM event WHERE action='audit'").fetchone()
    assert row["agent"] == "JeBobs"
    detail = json.loads(row["detail_json"])
    assert detail["b5_commit"] == "bbbb2222"
    assert detail["high-signal findings"] == "3 -> 1 (-2)"
    assert detail["closed"] == ["GameSource/World/A.cpp (-2)"]

    summary = store.audit_summary()
    assert [point["commit"] for point in summary["history"]] == ["aaaa1111", "bbbb2222"]
    assert summary["funcaudit"]["clean"] == 2


def test_tu_detail_and_dashboard_carry_the_audit(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    store.import_workflow(_workflow(tmp_path))
    client = TestClient(create_app(store))

    detail = client.get("/api/tu", params={"id": "GameSource/World/A.cpp"}).json()
    assert detail["audit"]["file"] == "GameSource/World/A.cpp"
    assert detail["audit"]["rollup"]["weight"] == 2
    assert detail["audit"]["funcs"]["BrnWorld::A::Run"]["weight"] == 2
    assert [s["name"] for s in detail["audit"]["stubs"]] == ["BrnWorld::A::Stop", "BrnWorld::A::Tick"]
    run = next(f for f in detail["funcs"] if f["name"] == "BrnWorld::A::Run")
    assert run["audit_weight"] == 2
    assert "MISSING_CASE" in run["audit_findings"]

    state = client.get("/dashboard/state").json()
    assert state["audit"]["funcaudit"]["verified_percent"] == 33.3
    assert state["audit"]["stubs"]["live"] == 1
    assert state["audit"]["history"][0]["commit"] == "aaaa1111"

    facets = client.get("/api/facets").json()
    assert "MISSING_CASE" in facets["audit_categories"]
    assert facets["stub_tiers"] == ["HIGH", "MEDIUM", "LOW"]

    assert client.get("/api/audit/summary").json()["funcaudit"]["paired"] == 3
    files = client.get("/api/audit/files", params={"category": "MISSING_CALLEE"}).json()
    assert files["total"] == 1
    fns = client.get("/api/audit/functions", params={"file": "GameSource/World/A.cpp"}).json()
    assert fns["items"][0]["name"] == "BrnWorld::A::Run"
    stub_files = client.get("/api/stubs/files", params={"live": "true"}).json()
    assert stub_files["items"][0]["file"] == "GameSource/World/A.cpp"
    stubs = client.get("/api/stubs", params={"file": "GameSource/World/A.cpp"}).json()
    assert stubs["rollup"]["console_lines"] == 300


def test_workflow_without_audit_files_imports_cleanly(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    root = _workflow(tmp_path)
    (root / "progress" / "funcaudit.json").unlink()
    (root / "progress" / "stubs.json").unlink()
    result = store.import_workflow(root)
    assert result["audit_findings"] == 0
    summary = store.audit_summary()
    assert summary["funcaudit"] == {"verified_percent": 0.0}
    assert summary["history"] == []


def test_segments_and_top_lists(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    store.import_workflow(_workflow(tmp_path))
    client = TestClient(create_app(store))

    summary = client.get("/api/audit/summary").json()
    # A::Run has high-signal findings, B::Run has no body; clean and unpaired come from stats.
    assert summary["segments"] == {"clean": 1, "high": 1, "soft": 0, "no_body": 1, "unpaired": 0}

    top = client.get("/api/audit/top", params={"category": "MISSING_CASE"}).json()
    assert [t["name"] for t in top["items"]] == ["BrnWorld::A::Run"]
    assert top["items"][0]["ids"] == 2   # "3, 4"

    live = client.get("/api/stubs/top", params={"live": "true"}).json()
    assert [t["name"] for t in live["items"]] == ["BrnWorld::A::Stop"]
    everything = client.get("/api/stubs/top", params={"live": "false"}).json()
    assert len(everything["items"]) == 2




def _asm_workflow(tmp_path, commit="aaaa1111"):
    """The audit checkout plus an instruction-shape audit of a build of that commit."""
    root = _workflow(tmp_path, commit=commit)
    (root / "progress" / "asmaudit.json").write_text(
        json.dumps(
            {
                "meta": {"tool": "asmaudit", "generated_at": "2026-09-20T04:00:00Z",
                         "b5_commit": commit, "b5_author": "Adriwin", "exe_commit": "eeee2222"},
                "stats": {"identity": 4, "no_export": 0, "paired_in_exe": 3, "not_in_exe": 1,
                          "A": 1, "B": 0, "C": 1, "T": 1, "scoreable": 2, "shape_percent": 50.0,
                          "mean_score": 65.0},
                "results": [
                    {"name": "BrnWorld::A::Run", "addr": "0x82000010", "file": "GameSource/World/A.cpp",
                     "pc_va": "0x140001000", "tier": "C", "score": 40.0,
                     "components": {"calls": 0.25, "cond": 0.5, "imm": 0.5, "ind": None},
                     "counts": {"n": [120, 80], "cond": [6, 3], "ind": [0, 0], "calls": [3, 1], "imm": [2, 2]},
                     "diff": {"calls_only_console": ["BrnWorld::RemoveRivals", "BrnWorld::Tick"],
                              "calls_only_pc": [], "calls_only_console_n": 2, "calls_only_pc_n": 0,
                              "imm_only_console": [39], "imm_only_pc": [64]},
                     "flags": {"total": 3, "kinds": {"PC boot gate": 2, "PC witness": 1}},
                     "notes": ["flagged in source: 2 PC boot gate, 1 PC witness"]},
                    {"name": "BrnWorld::A::Stop", "addr": "0x82000018", "file": "GameSource/World/A.cpp",
                     "pc_va": "0x140001200", "tier": "A", "score": 90.0,
                     "components": {"calls": 1.0, "cond": 1.0, "imm": 0.5, "ind": None},
                     "counts": {"n": [40, 30], "cond": [2, 2], "ind": [1, 1], "calls": [1, 1], "imm": [2, 1]},
                     "diff": {"calls_only_console": [], "calls_only_pc": [], "calls_only_console_n": 0,
                              "calls_only_pc_n": 0, "imm_only_console": [7], "imm_only_pc": []},
                     "notes": ["folded: 1 other symbol(s) share this PC address"]},
                    {"name": "BrnWorld::B::Run", "addr": "0x82000020", "file": "GameSource/World/B.cpp",
                     "pc_va": "0x140001400", "tier": "T", "score": None,
                     "components": {"calls": None, "cond": None, "imm": None, "ind": None},
                     "counts": {"n": [6, 4], "cond": [0, 0], "ind": [0, 0], "calls": [0, 0], "imm": [0, 0]},
                     "diff": {"calls_only_console": [], "calls_only_pc": [], "calls_only_console_n": 0,
                              "calls_only_pc_n": 0, "imm_only_console": [], "imm_only_pc": []},
                     "notes": []},
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


def test_asm_import_summary_files_functions_and_top(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    root = _asm_workflow(tmp_path)
    result = store.import_workflow(root)
    assert result["asm_functions"] == 3

    summary = store.audit_summary()
    assert summary["asm"]["A"] == 1
    assert summary["asm"]["shape_percent"] == 50.0
    assert summary["asm"]["files"] == 2
    assert summary["asm"]["commit"] == "aaaa1111@eeee2222"   # keyed per build: <b5>@<exe>
    assert summary["history"][-1]["asm_a"] == 1
    assert summary["history"][-1]["asm_shape_percent"] == 50.0

    files = store.asm_files()
    assert files["total"] == 2
    assert files["items"][0]["file"] == "GameSource/World/A.cpp"
    assert files["items"][0]["c"] == 1 and files["items"][0]["a"] == 1
    assert files["items"][0]["mean_score"] == 65.0
    assert [i["file"] for i in store.asm_files(tier="T")["items"]] == ["GameSource/World/B.cpp"]
    assert files["items"][0]["flagged"] == 1
    assert [i["file"] for i in store.asm_files(flagged_only=True)["items"]] == ["GameSource/World/A.cpp"]
    assert summary["asm"]["flagged"] == 1

    functions = store.asm_functions("GameSource/World/A.cpp")
    assert functions["tu_id"] == "GameSource/World/A.cpp"
    assert [f["tier"] for f in functions["items"]] == ["C", "A"]
    assert functions["items"][0]["diff"]["calls_only_console"] == ["BrnWorld::RemoveRivals", "BrnWorld::Tick"]
    assert functions["items"][0]["flags"]["kinds"] == {"PC boot gate": 2, "PC witness": 1}
    assert functions["items"][1]["notes"][0].startswith("folded")

    assert [t["name"] for t in store.asm_top("C")] == ["BrnWorld::A::Run"]

    # a re-import of the same build changes nothing; a new build logs a delta on tier C
    assert store.import_workflow(root)["asm_functions"] == 0

    client = TestClient(create_app(store))
    assert client.get("/api/asm/files").json()["total"] == 2
    assert client.get("/api/asm/files", params={"flagged": "true"}).json()["total"] == 1
    assert client.get("/api/asm/top", params={"tier": "C"}).json()["items"][0]["name"] == "BrnWorld::A::Run"
    assert client.get("/api/asm/functions", params={"file": "GameSource/World/A.cpp"}).json()["items"][0]["score"] == 40.0
    assert "asm_tiers" in client.get("/api/facets").json()
