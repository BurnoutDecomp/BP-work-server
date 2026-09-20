"""The evolution layer: one snapshot of every ring's totals per import, and the merged
time series the Evolution chart draws.

Only the console evidence (audit_run) carried a history before 2026-09-20; the three
ledger rings (translation units, functions, executable) were live totals with no past.
``record`` stores their totals on every workflow import; ``backfill`` rebuilds the past
from the workflow repo's own git history (status.json changed on 91 days between
2026-06-11 and 2026-09-19) by importing each day's last ledger into a throwaway store.
``points`` merges both sources into one list, oldest first, one point per snapshot or
audited commit, with every series the dashboard can toggle.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'import',
  commit_hash TEXT,
  metrics_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_snapshot_ts ON snapshot(ts);
"""

# the ledger files an import reads; the backfill extracts exactly these per commit
LEDGER_PATHS = (
    "progress/tu_index.json",
    "progress/status.json",
    "progress/tu_deps.json",
    "progress/goals.json",
    "progress/class_homes.json",
    "progress/unidentified.json",
)
BUILD_SCRIPT = "tools/build/build_game_exe.bat"
# the files whose change makes a day worth a snapshot
BACKFILL_TRIGGERS = ("progress/status.json", "progress/tu_index.json", "progress/unidentified.json", BUILD_SCRIPT)

# every series a point can carry, with the total it is a share of (None = counts only)
SERIES: dict[str, str | None] = {
    "tu_done": "tu_total",
    "tu_compiled": "tu_total",
    "tu_in_progress": "tu_total",
    "tu_blocked": "tu_total",
    "tu_todo": "tu_total",
    "tu_linked": "tu_total",
    "funcs_done": "funcs_total",
    "funcs_named_uncovered": "funcs_total",
    "funcs_unidentified": "funcs_total",
    "paired": "funcs_total",
    "clean": "paired",
    "no_body": "funcs_total",
    "weight": None,
    "stubs": None,
    "stubs_live": None,
    "asm_a": "asm_scoreable",
}


def metrics(con: sqlite3.Connection, linked_known: bool = True) -> dict[str, Any]:
    """The three rings' totals, computed exactly as the dashboard computes them."""
    # imported lazily: store imports this module
    from bp_work_server.schema import TU_STATUSES
    from bp_work_server.store import NOT_UNIDENTIFIED, NOT_UNIDENTIFIED_BARE

    counts = {key: 0 for key in TU_STATUSES}
    for row in con.execute(
        f"SELECT status, COUNT(*) AS c FROM tu WHERE {NOT_UNIDENTIFIED_BARE} GROUP BY status"
    ):
        counts[row["status"]] = row["c"]
    tu_total = sum(counts.values())
    funcs_total = con.execute("SELECT COUNT(*) FROM func").fetchone()[0]
    unidentified = con.execute(
        f"SELECT COUNT(*) FROM func f JOIN tu ON tu.id=f.tu_id WHERE NOT {NOT_UNIDENTIFIED}"
    ).fetchone()[0]
    done_funcs = con.execute("SELECT COUNT(*) FROM func WHERE status!='todo'").fetchone()[0]
    linked = con.execute(
        f"SELECT COUNT(*) FROM tu WHERE linked=1 AND {NOT_UNIDENTIFIED_BARE}"
    ).fetchone()[0]
    return {
        "tu_total": tu_total,
        "tu_done": counts.get("done", 0),
        "tu_compiled": counts.get("compiled", 0),
        "tu_in_progress": counts.get("in_progress", 0),
        "tu_blocked": counts.get("blocked", 0),
        "tu_todo": counts.get("todo", 0),
        "tu_linked": linked if linked_known else None,
        "funcs_total": funcs_total,
        "funcs_done": done_funcs,
        "funcs_named_uncovered": max(0, funcs_total - done_funcs - unidentified),
        "funcs_unidentified": unidentified,
    }


def record(
    con: sqlite3.Connection,
    ts: str,
    commit: str | None,
    source: str = "import",
    values: dict[str, Any] | None = None,
) -> bool:
    """Store a snapshot unless the newest one is from the same UTC day and already holds
    these exact totals: an import that changed nothing adds no point, but every day gets
    one (a flat line that reaches today reads "still true", a line that stops does not)."""
    con.executescript(SCHEMA)
    data = values if values is not None else metrics(con)
    last = con.execute("SELECT metrics_json, ts FROM snapshot ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
    if (
        last is not None
        and json.loads(last["metrics_json"]) == data
        and last["ts"] <= ts
        and _utc(last["ts"])[:10] == _utc(ts)[:10]
    ):
        return False
    con.execute(
        "INSERT INTO snapshot(ts, source, commit_hash, metrics_json) VALUES(?, ?, ?, ?)",
        (ts, source, commit, json.dumps(data, sort_keys=True)),
    )
    return True


def has_point_today(con: sqlite3.Connection, now: str) -> bool:
    """Whether a snapshot already exists for ``now``'s UTC day."""
    con.executescript(SCHEMA)
    today = _utc(now)[:10]
    for row in con.execute("SELECT ts FROM snapshot ORDER BY ts DESC, id DESC LIMIT 5"):
        if _utc(row["ts"])[:10] == today:
            return True
    return False


def seconds_until_daily_tick(now: datetime, hour: int = 0, minute: int = 10) -> float:
    """Seconds from ``now`` (aware) to the next HH:MM UTC, at least one second."""
    now = now.astimezone(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def points(con: sqlite3.Connection, days: int | None = None) -> list[dict[str, Any]]:
    """Snapshots and audit runs merged into one oldest-first series.

    Each point carries ``ts`` (ISO), ``date`` (YYYY-MM-DD), the ring totals of the
    snapshot and the evidence totals of the audit runs. Several snapshots or runs on
    one day stay separate points; a day with only one of the two sources carries the
    other source's most recent values, so every visible line is continuous."""
    con.executescript(SCHEMA)
    # stamps arrive with mixed offsets (git commit dates, server UTC): sort and date them in UTC
    # rows = (utc ts, point key, source, values): every snapshot is its own point, the
    # audit runs of one import (same stamp) share one
    rows: list[tuple[str, str, str, dict[str, Any]]] = []
    for row in con.execute("SELECT id, ts, commit_hash, metrics_json FROM snapshot ORDER BY ts, id"):
        ts = _utc(row["ts"])
        rows.append((ts, f"{ts}#s{row['id']}", "snapshot", {"commit": row["commit_hash"], **json.loads(row["metrics_json"])}))
    for row in con.execute("SELECT kind, commit_hash, imported_at, stats_json FROM audit_run ORDER BY imported_at, id"):
        stats = json.loads(row["stats_json"] or "{}")
        if row["kind"] == "funcaudit":
            values = {
                "b5_commit": row["commit_hash"],
                "paired": int(stats.get("paired") or 0),
                "clean": int(stats.get("clean") or 0),
                "no_body": int(stats.get("no_body") or 0),
                "weight": int(stats.get("weight") or 0),
            }
        elif row["kind"] == "asm":
            values = {
                "asm_a": int(stats.get("A") or 0),
                "asm_scoreable": int(stats.get("scoreable") or 0),
            }
        else:
            values = {
                "stubs": int(stats.get("stubs") or 0),
                "stubs_live": int(stats.get("live") or 0),
            }
        ts = _utc(row["imported_at"])
        rows.append((ts, f"{ts}#a", f"audit:{row['kind']}", values))
    rows.sort(key=lambda r: (r[0], r[1]))

    # carry each source's latest values forward so every line is continuous
    carried: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for ts, key, source, values in rows:
        carried.update(values)
        point = by_key.get(key)
        if point is None:
            point = {"ts": ts, "date": ts[:10], "sources": []}
            by_key[key] = point
            out.append(point)
        point["sources"].append(source)
        point.update(carried)
    if days:
        cutoff = _days_ago(days)
        out = [p for p in out if p["date"] >= cutoff]
    return out


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _utc(ts: str) -> str:
    """An ISO stamp rewritten in UTC (second precision); unparsable input is returned as is."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return str(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


async def daily_task(store: Any, log: Any = None) -> None:
    """Runs for the life of the process: a snapshot right away when today has none
    (a restart or a quiet day must not leave a hole), then one every day at 00:10 UTC.
    An import on the same day with the same totals adds nothing on top of it."""
    import asyncio

    while True:
        try:
            if await asyncio.to_thread(store.record_daily_snapshot):
                if log:
                    log.info("daily history snapshot recorded")
        except Exception:  # noqa: BLE001 -- a failed tick must not end the loop
            if log:
                log.exception("daily history snapshot failed")
        await asyncio.sleep(seconds_until_daily_tick(datetime.now(timezone.utc)))


# --------------------------------------------------------------------------- backfill
def backfill(repo: str | Path, since: str | None = None, out_path: str | Path | None = None,
             progress: Any = None) -> list[dict[str, Any]]:
    """Rebuild the ring history from the workflow repo: the last commit of every day
    that touched the ledger, imported into a throwaway store, one snapshot each.
    Returns the snapshots (``ts``, ``commit``, ``metrics``) and writes them as JSON
    when ``out_path`` is given, for ``import_file`` on the server."""
    from bp_work_server.store import WorkStore

    repo = Path(repo)
    log = _git(repo, "log", "--format=%H %cI", "--", *BACKFILL_TRIGGERS)
    per_day: dict[str, tuple[str, str]] = {}
    for line in log.splitlines():
        sha, ts = line.split()
        day = ts[:10]
        if since and day < since:
            continue
        per_day.setdefault(day, (sha, ts))  # newest first: the first seen wins
    snapshots: list[dict[str, Any]] = []
    for day in sorted(per_day):
        sha, ts = per_day[day]
        with tempfile.TemporaryDirectory(prefix="bp-history-") as tmp:
            root = Path(tmp) / "workflow"
            root.mkdir()
            present = _extract(repo, sha, root)
            if "progress/tu_index.json" not in present:
                continue
            store = WorkStore(Path(tmp) / "work.sqlite3")
            store.migrate()
            store.import_workflow(root, record_history=False)
            with store.connect() as con:
                values = metrics(con, linked_known=BUILD_SCRIPT in present)
        snapshots.append({"ts": ts, "commit": sha, "metrics": values})
        if progress:
            progress(day, sha, values)
    if out_path:
        Path(out_path).write_text(json.dumps({"snapshots": snapshots}, indent=1), encoding="utf-8")
    return snapshots


def import_file(con: sqlite3.Connection, path: str | Path) -> int:
    """Load a backfill file; a commit already present is skipped."""
    con.executescript(SCHEMA)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    have = {row[0] for row in con.execute("SELECT commit_hash FROM snapshot WHERE commit_hash IS NOT NULL")}
    n = 0
    for snap in data.get("snapshots", []):
        if snap["commit"] in have:
            continue
        con.execute(
            "INSERT INTO snapshot(ts, source, commit_hash, metrics_json) VALUES(?, 'backfill', ?, ?)",
            (snap["ts"], snap["commit"], json.dumps(snap["metrics"], sort_keys=True)),
        )
        have.add(snap["commit"])
        n += 1
    return n


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def _extract(repo: Path, sha: str, root: Path) -> set[str]:
    """Check the ledger files and the build script of one commit out into ``root``.
    Returns the paths that exist at that commit."""
    wanted = [*LEDGER_PATHS, BUILD_SCRIPT]
    listing = _git(repo, "ls-tree", "-r", "--name-only", sha, "--", *wanted)
    present = {line.strip() for line in listing.splitlines() if line.strip()}
    if not present:
        return present
    archive = root.parent / f"{sha[:12]}.tar"
    with open(archive, "wb") as fh:
        proc = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", sha, "--", *sorted(present)],
            stdout=fh, stderr=subprocess.PIPE, check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"git archive {sha} failed: {proc.stderr.decode(errors='ignore').strip()}")
    with tarfile.open(archive) as tar:
        tar.extractall(root)
    archive.unlink()
    return present
