"""Byte-match measurements the work server runs for CI: requests the web app queues and a root
runner (deploy/bytematch/runner.py) carries out.

The web app never runs a compiler or Docker. It drops a request naming one native commit into
the queue and serves the files the runner writes back:

  queue/<sha>.json            a request (the runner removes it when it takes it)
  jobs/<sha>/status.json      {"sha", "state", "progress", "error", "result", ...}
  jobs/<sha>/bundle.json.gz   the records and state CI adopts (tools/bytematch/remote.py)

A state is queued, running, done, failed or superseded (the commit stopped being the tip of
the native branch before it was measured; CI would not publish it). BP_BYTEMATCH_DIR names the
folder (default /var/lib/bp-work-server/bytematch).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from bp_work_server.store import iso

SHA = re.compile(r"[0-9a-f]{40}")
ACTIVE = ("queued", "running", "done", "superseded")
MAX_QUEUED = 32
# The runner refreshes a running job's status at least every 5 minutes, and takes a request
# off the queue as it starts it. A job silent for longer was cut off and may be queued again.
STALE_RUNNING, STALE_QUEUED = 30 * 60, 10 * 60


def root() -> Path:
    return Path(os.environ.get("BP_BYTEMATCH_DIR", "/var/lib/bp-work-server/bytematch"))


def check_sha(sha: str) -> str:
    sha = (sha or "").strip().lower()
    if not SHA.fullmatch(sha):
        raise ValueError("sha must be a full 40-character commit id")
    return sha


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def status(sha: str, base: Path | None = None) -> dict | None:
    path = (base or root()) / "jobs" / check_sha(sha) / "status.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:   # mid-replace on a filesystem without atomic rename
        return {"sha": sha, "state": "running", "progress": "updating"}


def _age(current: dict) -> float:
    stamp = current.get("updated_at") or current.get("requested_at")
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def cut_off(current: dict, base: Path) -> bool:
    """A job whose runner stopped: running and silent, or queued with its request gone."""
    state = current.get("state")
    if state == "running":
        return _age(current) > STALE_RUNNING
    if state == "queued":
        return not (base / "queue" / f"{current.get('sha')}.json").exists() and _age(current) > STALE_QUEUED
    return False


def request(sha: str, by: str, base: Path | None = None) -> tuple[dict, bool]:
    """Queue a measurement unless one is queued, running or finished: (status, newly queued).
    A failed or cut-off measurement is queued again."""
    sha, base = check_sha(sha), base or root()
    current = status(sha, base)
    if current and current.get("state") in ACTIVE and not cut_off(current, base):
        return current, False
    queue = base / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    if len(list(queue.glob("*.json"))) >= MAX_QUEUED:
        raise RuntimeError("the byte-match queue is full")
    now = iso()
    _write(base / "jobs" / sha / "status.json", {"sha": sha, "state": "queued", "requested_at": now, "by": by})
    _write(queue / f"{sha}.json", {"sha": sha, "requested_at": now, "by": by})
    return status(sha, base), True


def bundle(sha: str, base: Path | None = None) -> Path | None:
    """The finished measurement's bundle, or None while there is none."""
    sha, base = check_sha(sha), base or root()
    current = status(sha, base)
    path = base / "jobs" / sha / "bundle.json.gz"
    return path if current and current.get("state") == "done" and path.is_file() else None
