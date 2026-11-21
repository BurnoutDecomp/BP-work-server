from __future__ import annotations

import gzip
import json

from fastapi.testclient import TestClient

from bp_work_server import bytematch_jobs
from bp_work_server.api import create_app
from bp_work_server.store import WorkStore

SHA = "cdc74d376fc11395b4698780d1c63f5f2b5fe140"


def client_for(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_BYTEMATCH_DIR", str(tmp_path / "bytematch"))
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    return TestClient(create_app(store)), store


def runner_writes(tmp_path, sha, **status):
    folder = tmp_path / "bytematch" / "jobs" / sha
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "status.json").write_text(json.dumps({"sha": sha, **status}), encoding="utf-8")
    return folder


def test_request_queues_once_and_reports_the_same_job(tmp_path):
    base = tmp_path / "bm"
    status, queued = bytematch_jobs.request(SHA.upper(), "ci", base)
    assert queued and status["state"] == "queued" and status["sha"] == SHA
    assert (base / "queue" / f"{SHA}.json").is_file()
    again, queued = bytematch_jobs.request(SHA, "ci", base)
    assert not queued and again["state"] == "queued"
    assert len(list((base / "queue").glob("*.json"))) == 1


def test_a_failed_job_is_queued_again_and_a_finished_one_is_not(tmp_path):
    base = tmp_path / "bm"
    folder = base / "jobs" / SHA
    folder.mkdir(parents=True)
    (folder / "status.json").write_text(json.dumps({"sha": SHA, "state": "failed"}), encoding="utf-8")
    status, queued = bytematch_jobs.request(SHA, "ci", base)
    assert queued and status["state"] == "queued"
    (base / "queue" / f"{SHA}.json").unlink()
    (folder / "status.json").write_text(json.dumps({"sha": SHA, "state": "done"}), encoding="utf-8")
    status, queued = bytematch_jobs.request(SHA, "ci", base)
    assert not queued and status["state"] == "done"


def test_a_job_whose_runner_died_is_queued_again(tmp_path):
    base = tmp_path / "bm"
    folder = base / "jobs" / SHA
    folder.mkdir(parents=True)
    (folder / "status.json").write_text(json.dumps(
        {"sha": SHA, "state": "running", "updated_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    status, queued = bytematch_jobs.request(SHA, "ci", base)
    assert queued and status["state"] == "queued"
    # a live one (just updated) is left alone
    (base / "queue" / f"{SHA}.json").unlink()
    (folder / "status.json").write_text(json.dumps(
        {"sha": SHA, "state": "running", "updated_at": bytematch_jobs.iso()}), encoding="utf-8")
    status, queued = bytematch_jobs.request(SHA, "ci", base)
    assert not queued and status["state"] == "running"


def test_a_queued_job_that_lost_its_request_is_queued_again(tmp_path):
    base = tmp_path / "bm"
    folder = base / "jobs" / SHA
    folder.mkdir(parents=True)
    (folder / "status.json").write_text(json.dumps(
        {"sha": SHA, "state": "queued", "requested_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    _status, queued = bytematch_jobs.request(SHA, "ci", base)
    assert queued and (base / "queue" / f"{SHA}.json").is_file()


def test_requests_name_full_commits_only(tmp_path):
    for bad in ("", "cdc74d3", SHA + "0", "../../etc/passwd", "g" * 40):
        try:
            bytematch_jobs.request(bad, "ci", tmp_path)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert not (tmp_path / "queue").exists()


def test_the_bundle_exists_only_once_the_job_is_done(tmp_path):
    base = tmp_path / "bm"
    folder = base / "jobs" / SHA
    folder.mkdir(parents=True)
    (folder / "bundle.json.gz").write_bytes(gzip.compress(b"{}"))
    (folder / "status.json").write_text(json.dumps({"sha": SHA, "state": "running"}), encoding="utf-8")
    assert bytematch_jobs.bundle(SHA, base) is None
    (folder / "status.json").write_text(json.dumps({"sha": SHA, "state": "done"}), encoding="utf-8")
    assert bytematch_jobs.bundle(SHA, base) == folder / "bundle.json.gz"


def test_the_measure_routes_need_an_admin(tmp_path, monkeypatch):
    client, store = client_for(tmp_path, monkeypatch)
    worker = store.create_worker("someone", is_admin=False)
    for headers in ({}, {"X-Work-Token": worker["token"]}):
        assert client.post("/admin/bytematch/measure", json={"sha": SHA}, headers=headers).status_code in (401, 403)
        assert client.get(f"/admin/bytematch/measure/{SHA}", headers=headers).status_code in (401, 403)
        assert client.get(f"/admin/bytematch/measure/{SHA}/bundle", headers=headers).status_code in (401, 403)
    assert not (tmp_path / "bytematch" / "queue").exists()


def test_ci_queues_polls_and_collects_a_measurement(tmp_path, monkeypatch):
    client, store = client_for(tmp_path, monkeypatch)
    admin = {"X-Work-Token": store.create_worker("ci-build", is_admin=True)["token"]}
    assert client.get(f"/admin/bytematch/measure/{SHA}", headers=admin).status_code == 404
    response = client.post("/admin/bytematch/measure", json={"sha": SHA}, headers=admin)
    assert response.status_code == 202 and response.json()["state"] == "queued"
    assert client.post("/admin/bytematch/measure", json={"sha": SHA}, headers=admin).status_code == 200
    assert client.post("/admin/bytematch/measure", json={"sha": "nope"}, headers=admin).status_code == 400
    assert client.get(f"/admin/bytematch/measure/{SHA}/bundle", headers=admin).status_code == 404

    folder = runner_writes(tmp_path, SHA, state="done", result={"measured": 2})
    payload = gzip.compress(json.dumps({"schema": 1, "sha": SHA}).encode())
    (folder / "bundle.json.gz").write_bytes(payload)
    status = client.get(f"/admin/bytematch/measure/{SHA}", headers=admin).json()
    assert status["state"] == "done" and status["result"]["measured"] == 2
    bundle = client.get(f"/admin/bytematch/measure/{SHA}/bundle", headers=admin)
    assert bundle.status_code == 200 and bundle.content == payload
    assert bundle.headers["content-type"] == "application/gzip"
