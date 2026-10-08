import json
import sqlite3

from fastapi.testclient import TestClient

from bp_work_server import history
from bp_work_server.api import create_app
from bp_work_server.build_providers import read_build_providers
from bp_work_server.schema import WORK_SCHEMA
from bp_work_server.store import WorkStore, iso


SCRIPT = r'''@echo off
set ROOT=%~dp0..\..
set SRC=%ROOT%\b5-decomp\src
set VEN=%ROOT%\b5-decomp\vendor
call "%~dp0msvc_env.bat"
(
 echo "%SRC%\Original.cpp"
 echo "%SRC%\D3D.cpp"
 echo "%SRC%\pc\gcm\renderengine\XenonD3D9Shims.cpp"
 echo "%SRC%\GameShared\GameClasses\System\PC\CgsAudioOutputPC.cpp"
 echo "%VEN%\dirtysdk\src\pc\lan\pclan_core.cpp"
 echo "%VEN%\dirtysdk\src\pc\lan\pclan_link.cpp"
)
:driver_compile
%PY_CMD% "%~dp0compile_exe.py" --rsp "%RSP%" -- d3d9.lib user32.lib kernel32.lib ntdll.lib ws2_32.lib "%VEN%\lua\lua515.lib"
if "%BUILD_ERR%"=="0" copy /Y "%XA2%\bin\x64\xaudio2_9redist.dll" "%OUT%\" >nul
'''


def workflow(tmp_path, text=SCRIPT):
    root = tmp_path / 'workflow'
    script = root / 'tools/build/build_game_exe.bat'
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(text, encoding='utf-8')
    return root


def test_only_confirmed_current_providers_are_counted(tmp_path):
    providers = read_build_providers(workflow(tmp_path))
    assert len(providers) == 12
    assert providers['vendor:lua']['kind'] == 'static_library'
    assert providers['class:XAUDIO::CEngine']['kind'] == 'runtime_library'
    assert all(p['evidence'] for p in providers.values())
    assert 'vendor:crypto' not in providers
    assert 'class:D3D::CCapture' not in providers
    assert 'class:XAUDIO::CReverbEffect' not in providers


def test_comments_source_availability_and_dependency_checks_are_not_link_evidence(tmp_path):
    text = SCRIPT.replace('%PY_CMD% "%~dp0compile_exe.py"', 'rem %PY_CMD% "%~dp0compile_exe.py"')
    text += '\nif not exist "%VEN%\\lua\\lua515.lib" exit /b 1\n'
    providers = read_build_providers(workflow(tmp_path, text))
    assert 'vendor:lua' not in providers and 'vendor:xbox-d3d' not in providers
    assert 'vendor:compiler-rt' not in providers
    assert read_build_providers(tmp_path / 'missing') is None


def test_removing_backends_libraries_or_runtime_staging_revokes_evidence(tmp_path):
    root = workflow(tmp_path, SCRIPT.replace('d3d9.lib', 'other.lib').replace('copy /Y', 'rem copy /Y'))
    providers = read_build_providers(root)
    assert 'class:D3D' not in providers and 'class:XGRAPHICS::CFG' not in providers
    assert 'class:XAUDIO::CEngine' not in providers
    script = root / 'tools/build/build_game_exe.bat'
    script.write_text(SCRIPT.replace(' echo "%VEN%\\dirtysdk\\src\\pc\\lan\\pclan_link.cpp"', ''), encoding='utf-8')
    assert 'vendor:ea-dirtysdk' not in read_build_providers(root)


def test_available_union_is_consistent_and_never_changes_linked_or_reconstruction(tmp_path):
    store = WorkStore(tmp_path / 'work.sqlite3')
    store.migrate()
    with store.connect() as con:
        for tu, source, status, dest in [
            ('Original', 'decfigs', 'done', 'b5-decomp/src/Original.cpp'),
            ('vendor:lua', 'vendor', 'external', None),
            ('class:D3D', 'class', 'external', 'b5-decomp/src/D3D.cpp'),
            ('vendor:crypto', 'vendor', 'external', None),
            ('OtherExternal', 'class', 'external', None),
            ('unidentified:console', 'unidentified', 'todo', 'b5-decomp/src/Original.cpp'),
        ]:
            con.execute('INSERT INTO tu(id,source,status,dest_path,updated_at) VALUES(?,?,?,?,?)',
                        (tu, source, status, dest, iso()))
            con.execute('INSERT INTO func(name,tu_id,status) VALUES(?,?,?)',
                        (tu + '::Run', tu, 'reviewed' if tu == 'Original' else 'todo'))
        before = [tuple(r) for r in con.execute('SELECT id,status,owner,notes,updated_at FROM tu ORDER BY id')]
        store._restore_linked(con, workflow(tmp_path))
        assert before == [tuple(r) for r in con.execute('SELECT id,status,owner,notes,updated_at FROM tu ORDER BY id')]
        assert con.execute("SELECT linked FROM tu WHERE id='vendor:lua'").fetchone()[0] == 0
    dashboard = store.dashboard_state()['totals']
    compact = store.progress_map()
    assert dashboard['linked_tus'] == compact['totals']['linked_tus'] == 2
    assert dashboard['source_linked_tus'] == compact['totals']['source_linked_tus'] == 1
    assert dashboard['external_build_tus'] == compact['totals']['external_build_tus'] == 2
    assert dashboard['available_tus'] == compact['totals']['available_tus'] == 3
    assert dashboard['available_percent'] == 60
    with store.connect() as con:
        data = history.metrics(con)
        assert (data['tu_linked'], data['tu_source_linked'], data['tu_external_build'], data['tu_available']) == (2, 1, 2, 3)
        assert con.execute("SELECT status FROM func WHERE tu_id='Original'").fetchone()[0] == 'reviewed'
        assert con.execute('SELECT COUNT(*) FROM event').fetchone()[0] == 0
    client = TestClient(create_app(store))
    listed = {r['id']: r for r in client.get('/api/tus').json()['items']}
    assert listed['Original']['linked'] is True  # Also fixes list rows losing the stored linked field.
    assert listed['vendor:lua']['linked'] is False
    assert listed['vendor:lua']['external_build_provider']['name'] == 'Lua 5.1.5'
    assert not listed['vendor:crypto']['external_build_provider']
    assert store.tu_detail('vendor:lua')['external_build_provider']['evidence']
    with store.connect() as con:
        store._restore_linked(con, tmp_path / 'unavailable-checkout')
        assert con.execute("SELECT build_provider FROM tu WHERE id='vendor:lua'").fetchone()[0]
        script = tmp_path / 'workflow/tools/build/build_game_exe.bat'
        script.write_text(SCRIPT.replace('lua515.lib', 'removed.lib'), encoding='utf-8')
        store._restore_linked(con, tmp_path / 'workflow')
        assert con.execute("SELECT build_provider FROM tu WHERE id='vendor:lua'").fetchone()[0] is None


def test_additive_migration_keeps_existing_claims_and_reviews(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as con:
        con.executescript(WORK_SCHEMA.replace('  build_provider TEXT,\n', ''))
        con.execute("INSERT INTO tu(id,status,owner,notes) VALUES('Claim','compiled','worker','review pending')")
        con.execute("INSERT INTO func(name,tu_id,status) VALUES('Claim::Run','Claim','reviewed')")
    store = WorkStore(path)
    store.migrate()
    store.migrate()
    with store.connect() as con:
        row = con.execute('SELECT * FROM tu').fetchone()
        assert (row['status'], row['owner'], row['notes'], row['build_provider']) == ('compiled', 'worker', 'review pending', None)
        assert con.execute('SELECT status FROM func').fetchone()[0] == 'reviewed'
