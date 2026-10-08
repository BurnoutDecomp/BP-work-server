import sqlite3

import pytest
from fastapi.testclient import TestClient

from bp_work_server import history, source_status
from bp_work_server.api import create_app
from bp_work_server.schema import WORK_SCHEMA
from bp_work_server.store import WorkStore, iso


def make_store(tmp_path):
    store = WorkStore(tmp_path / 'external.sqlite3')
    store.migrate()
    with store.connect() as con:
        con.executemany('INSERT INTO tu(id,status,updated_at) VALUES(?,?,?)',
                        [('Game', 'todo', iso()), ('Vendor', 'blocked', iso())])
        con.executemany('INSERT INTO func(name,tu_id,status) VALUES(?,?,?)',
                        [('Game::Run', 'Game', 'todo'), ('Vendor::Source', 'Vendor', 'todo'),
                         ('Vendor::Recovered', 'Vendor', 'reviewed')])
        con.execute("INSERT INTO tu_dep VALUES('Game','Vendor',1)")
        con.execute("INSERT INTO goal(name) VALUES('boot')")
        con.executemany("INSERT INTO goal_tu VALUES('boot',?)", [('Game',), ('Vendor',)])
    return store


def test_migrate_old_check_preserves_children_indexes_events_and_claim(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as con:
        con.executescript(WORK_SCHEMA.replace("'blocked','external'", "'blocked'"))
        con.execute("INSERT INTO tu(id,status,owner,lease_expires_at,notes) VALUES('Live','compiled','worker','2030-01-01','review pending')")
        con.execute("INSERT INTO tu(id,status) VALUES('Vendor','blocked')")
        con.execute("INSERT INTO func VALUES('Live::Run','Live','compiles','worker','2026-01-01')")
        con.execute("INSERT INTO tu_dep VALUES('Live','Vendor',9)")
        con.execute("INSERT INTO goal(name) VALUES('boot')")
        con.execute("INSERT INTO goal_tu VALUES('boot','Live')")
        con.execute("INSERT INTO event(ts,tu_id,action) VALUES('2026-01-01','Live','compiled')")
        con.execute("CREATE INDEX custom_tu_notes ON tu(notes)")
    store = WorkStore(path)
    store.migrate()
    store.migrate()  # idempotent
    with store.connect() as con:
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
        assert dict(con.execute("SELECT status,owner,notes FROM tu WHERE id='Live'").fetchone()) == {
            'status': 'compiled', 'owner': 'worker', 'notes': 'review pending'}
        assert tuple(con.execute('SELECT * FROM tu_dep').fetchone()) == ('Live', 'Vendor', 9)
        assert con.execute('SELECT COUNT(*) FROM func').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM goal_tu').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM event').fetchone()[0] == 1
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name='custom_tu_notes'").fetchone()
        con.execute("UPDATE tu SET status='external' WHERE id='Vendor'")


def test_external_is_supplied_dependency_and_separate_from_reconstruction(tmp_path):
    store = make_store(tmp_path)
    store.mark_external('Vendor', 'maintainer', 'Existing vendor source and Windows backend')
    state = store.dashboard_state()
    assert state['counts']['external'] == 1 and state['counts']['blocked'] == 0
    assert state['totals']['done_tus'] == 0
    assert state['totals']['done_funcs'] == state['totals']['external_funcs'] == 1
    assert state['totals']['func_percent'] == 33.33
    assert store.next_tus()[1][0].unresolved_deps == 0
    goal = store.goal_detail('boot')
    assert goal['external'] == goal['remaining_count'] == 1
    assert [r['id'] for r in goal['ready']] == ['Game']
    assert not store.claim('Vendor', 'worker', force=True).claimed
    snapshot = store.progress_map(include_functions=True)
    assert snapshot['totals']['external_tus'] == snapshot['totals']['external_funcs'] == 1
    assert snapshot['totals']['done_funcs'] == 1
    vendor = next(r for r in snapshot['units'] if r['id'] == 'Vendor')
    assert vendor['recorded_funcs'] == vendor['external_funcs'] == 1
    with store.connect() as con:
        metrics = history.metrics(con)
        assert metrics['tu_external'] == metrics['funcs_external'] == metrics['funcs_done'] == 1
        assert metrics['funcs_named_uncovered'] == 1
        assert con.execute("SELECT COUNT(*) FROM event WHERE action='review_pass'").fetchone()[0] == 0


def test_reimport_and_source_refresh_preserve_external_decision(tmp_path):
    store = make_store(tmp_path)
    store.mark_external('Vendor', 'maintainer', 'Existing vendor source')
    exported = store.export_status()
    assert exported['tu']['Vendor']['status'] == 'external'
    assert exported['func']['Vendor::Source']['status'] == 'external'
    with store.connect() as con:
        store._restore_status(con, {'tu': {'Vendor': {'status': 'blocked'}},
                                    'func': {'Vendor::Source': {'status': 'recovered'}}})
        assert con.execute("SELECT status FROM tu WHERE id='Vendor'").fetchone()[0] == 'external'
        assert con.execute("SELECT status FROM func WHERE name='Vendor::Source'").fetchone()[0] == 'external'
        con.execute("INSERT INTO func(name,tu_id) VALUES('Vendor::New','Vendor')")
        store._sync_external_functions(con)
        assert con.execute("SELECT status FROM func WHERE name='Vendor::New'").fetchone()[0] == 'external'
    proof = {'digest': 'd' * 64, 'file': 'Vendor.cpp', 'line': 1}
    evidence = {'version': 1, 'source_commit': 'a' * 40, 'base_source_commit': None,
                'functions': {'Vendor::Source': proof, 'Vendor::Recovered': proof, 'Vendor::New': proof},
                'tus': {'Vendor': {'digest': 'e' * 64, 'functions': ['Vendor::Source', 'Vendor::Recovered', 'Vendor::New']}}}
    result = source_status.apply(store, evidence)
    assert result['functions_recovered'] == result['tus_completed'] == 0
    assert store.export_status()['tu']['Vendor']['status'] == 'external'


def test_external_endpoint_and_live_work_protection(tmp_path, monkeypatch):
    monkeypatch.setenv('BP_WORK_REQUIRE_TOKEN', '0')
    store = make_store(tmp_path)
    client = TestClient(create_app(store))
    response = client.post('/tu/Vendor/external', json={'agent': 'maintainer', 'reason': 'Vendor source'})
    assert response.status_code == 204
    assert client.get('/snapshot').json()['counts']['external'] == 1
    assert 'external' in client.get('/api/facets').json()['tu_statuses']
    store.claim('Game', 'worker')
    with pytest.raises(ValueError, match='active or compiled'):
        store.mark_external('Game', 'maintainer', 'Wrong live transition')
    store.unblock('Vendor', 'maintainer')
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM func WHERE status='external'").fetchone()[0] == 0
