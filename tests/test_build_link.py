from __future__ import annotations

import json

from bp_work_server.build_link import dest_candidates, is_linked, parse_build_sources
from bp_work_server.store import WorkStore, iso


BUILD_SCRIPT = r"""@echo off
rem Build the real-chain game exe.
setlocal
set ROOT=%~dp0..\..
set SRC=%ROOT%\b5-decomp\src
set VEN=%ROOT%\b5-decomp\vendor
set OUT=%ROOT%\build\game
set "VCVARS="

(
  echo /nologo /EHsc /std:c++17
  echo /I"%SRC%" /I"%VEN%\EASTL\include"
  echo "%SRC%\GameSource\Main\BrnMain.cpp"
  echo "%SRC%\GameShared\GameClasses\Core\.\CgsAssert.cpp"
  rem echo "%SRC%\GameSource\NotYet\Parked.cpp"
  echo "%SRC%\GameSource\Main\BrnMain.cpp"
  echo "%VEN%\coreallocator\source\icoreallocator_interface.cpp"
  echo /Fo"%OUT%\obj\\" /Fe"%OUT%\Burnout_PC.exe"
) > "%OUT%\obj\build.rsp"
"""


def write_script(tmp_path, text: str = BUILD_SCRIPT):
    script = tmp_path / "tools" / "build" / "build_game_exe.bat"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(text, encoding="utf-8")
    return tmp_path


def test_parse_build_sources_resolves_vars_dedupes_and_skips_comments(tmp_path):
    root = write_script(tmp_path)

    assert parse_build_sources(root) == {
        "b5-decomp/src/GameSource/Main/BrnMain.cpp",
        "b5-decomp/src/GameShared/GameClasses/Core/CgsAssert.cpp",
        "b5-decomp/vendor/coreallocator/source/icoreallocator_interface.cpp",
    }


def test_parse_build_sources_resolves_canonical_absolute_root_assignment(tmp_path):
    script = BUILD_SCRIPT.replace(
        r"set ROOT=%~dp0..\..",
        r'for %%I in ("%~dp0..\..") do set "ROOT=%%~fI"',
    )
    root = write_script(tmp_path, script)

    assert parse_build_sources(root) == {
        "b5-decomp/src/GameSource/Main/BrnMain.cpp",
        "b5-decomp/src/GameShared/GameClasses/Core/CgsAssert.cpp",
        "b5-decomp/vendor/coreallocator/source/icoreallocator_interface.cpp",
    }


def test_parse_build_sources_honors_filters_before_incremental_driver(tmp_path):
    script = BUILD_SCRIPT.replace(
        'set OUT=%ROOT%\\build\\game',
        'set OUT=%ROOT%\\build\\game\nset RSP=%OUT%\\obj\\build.rsp',
    ).replace(
        ') > "%OUT%\\obj\\build.rsp"',
        ') > "%RSP%"\n'
        'findstr /v /c:"CgsAssert.cpp" "%RSP%" > "%RSP%.tmp"\n'
        'move /y "%RSP%.tmp" "%RSP%" >nul\n'
        ':driver_compile\n'
        'python compile_exe.py --rsp "%RSP%"',
    )
    root = write_script(tmp_path, script)

    assert parse_build_sources(root) == {
        "b5-decomp/src/GameSource/Main/BrnMain.cpp",
        "b5-decomp/vendor/coreallocator/source/icoreallocator_interface.cpp",
    }


def test_parse_build_sources_ignores_legacy_only_response_file_filters(tmp_path):
    script = BUILD_SCRIPT.replace(
        ') > "%OUT%\\obj\\build.rsp"',
        ') > "%OUT%\\obj\\build.rsp"\n'
        ':driver_compile\n'
        'python compile_exe.py --rsp "%RSP%"\n'
        ':legacy_compile\n'
        'findstr /v /c:"CgsAssert.cpp" "%RSP%" > "%RSP%.tmp"',
    )
    root = write_script(tmp_path, script)

    assert "b5-decomp/src/GameShared/GameClasses/Core/CgsAssert.cpp" in parse_build_sources(root)


def test_parse_build_sources_missing_script_is_unknown_not_empty_build(tmp_path):
    assert parse_build_sources(tmp_path) == set()


def test_dest_candidates_falls_back_to_the_cpp_a_header_decompiles_into():
    assert dest_candidates("b5-decomp/src/GameShared/./Core/CgsArray.h") == [
        "b5-decomp/src/GameShared/Core/CgsArray.h",
        "b5-decomp/src/GameShared/Core/CgsArray.cpp",
    ]
    assert dest_candidates(None) == []


def test_is_linked_matches_header_tus_through_their_cpp():
    sources = {"b5-decomp/src/GameSource/Main/BrnMain.cpp"}

    assert is_linked("b5-decomp/src/GameSource/Main/BrnMain.h", sources)
    assert is_linked("b5-decomp/src/GameSource/Main/BrnMain.cpp", sources)
    assert not is_linked("b5-decomp/src/GameSource/Main/BrnOther.cpp", sources)


def make_workflow(tmp_path, *, with_script: bool = True):
    root = tmp_path / "workflow"
    progress = root / "progress"
    progress.mkdir(parents=True)
    progress.joinpath("tu_index.json").write_text(
        json.dumps(
            {
                # linked: it is the compile line verbatim
                "GameSource/Main/BrnMain.cpp": {
                    "source": "decfigs",
                    "n_funcs": 1,
                    "functions": ["BrnMain"],
                },
                # linked: header decompiled into a compiled .cpp
                "GameShared/GameClasses/Core/CgsAssert.h": {
                    "source": "decfigs",
                    "n_funcs": 1,
                    "functions": ["CgsAssert::Fail"],
                },
                # not linked: nothing on the compile line builds it
                "GameSource/Parked/Parked.cpp": {
                    "source": "decfigs",
                    "n_funcs": 1,
                    "functions": ["Parked::Run"],
                },
                # linked through its resolved class home
                "class:Allocator": {"source": "class", "n_funcs": 1, "functions": ["Allocator::New"]},
            }
        ),
        encoding="utf-8",
    )
    progress.joinpath("class_homes.json").write_text(
        json.dumps(
            {"class:Allocator": "b5-decomp/vendor/coreallocator/source/icoreallocator_interface.cpp"}
        ),
        encoding="utf-8",
    )
    if with_script:
        write_script(root)
    return root


def test_import_flags_tus_the_game_build_compiles(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()

    result = store.import_workflow(make_workflow(tmp_path))

    assert result["linked"] == 3
    totals = store.dashboard_state()["totals"]
    assert totals["linked_tus"] == 3
    assert totals["linked_percent"] == 75.0
    with store.connect() as con:
        unlinked = [
            row["id"] for row in con.execute("SELECT id FROM tu WHERE linked=0 ORDER BY id")
        ]
    assert unlinked == ["GameSource/Parked/Parked.cpp"]


def test_import_without_a_build_script_keeps_the_previous_flags(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    root = make_workflow(tmp_path)
    store.import_workflow(root)

    (root / "tools" / "build" / "build_game_exe.bat").unlink()
    result = store.import_workflow(root)

    assert result["linked"] == 0
    assert store.dashboard_state()["totals"]["linked_tus"] == 3


def test_import_clears_tus_dropped_from_the_compile_line(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    root = make_workflow(tmp_path)
    store.import_workflow(root)

    # BrnMain is echoed twice in the fixture; both copies have to go.
    write_script(root, BUILD_SCRIPT.replace('  echo "%SRC%\\GameSource\\Main\\BrnMain.cpp"\n', ""))
    store.import_workflow(root)

    with store.connect() as con:
        linked = [row["id"] for row in con.execute("SELECT id FROM tu WHERE linked=1 ORDER BY id")]
    assert "GameSource/Main/BrnMain.cpp" not in linked
    assert len(linked) == 2


def test_import_clears_a_stale_class_home_dropped_from_the_current_map(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    root = make_workflow(tmp_path)
    store.import_workflow(root)

    (root / "progress" / "class_homes.json").write_text("{}", encoding="utf-8")
    store.import_workflow(root)

    with store.connect() as con:
        allocator = con.execute(
            "SELECT dest_path, linked FROM tu WHERE id='class:Allocator'"
        ).fetchone()
    assert allocator["dest_path"] == "b5-decomp/src/classes/Allocator.cpp"
    assert allocator["linked"] == 0
    assert store.dashboard_state()["totals"]["linked_tus"] == 2


def test_migrate_adds_linked_to_a_pre_existing_tu_table(tmp_path):
    import sqlite3

    from bp_work_server.schema import WORK_SCHEMA

    db = tmp_path / "legacy.sqlite3"
    legacy = "\n".join(
        line for line in WORK_SCHEMA.splitlines() if "linked" not in line and "compile line" not in line
    )
    con = sqlite3.connect(db)
    con.executescript(legacy)
    con.execute(
        "INSERT INTO tu(id, source, status, dest_path, updated_at) VALUES('t','decfigs','done','b5-decomp/src/T.cpp',?)",
        (iso(),),
    )
    con.commit()
    con.close()

    store = WorkStore(db)
    store.migrate()

    with store.connect() as con:
        assert "linked" in {row["name"] for row in con.execute("PRAGMA table_info(tu)")}
    assert store.dashboard_state()["totals"]["linked_tus"] == 0
